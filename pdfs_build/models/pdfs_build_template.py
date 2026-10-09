import base64
import json
import re
from urllib.parse import quote

import requests
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

DEFAULT_API_URL = "https://api.pdfs.build"
APP_URL = "https://app.pdfs.build"
# Placeholder the skeleton uses for line items; replaced once the model is known.
LINES_PLACEHOLDER = "record.line_ids  # TODO: the lines field, e.g. order_line"


class PdfsBuildTemplate(models.Model):
    _name = "pdfs_build.template"
    _description = "pdfs.build Template"
    _order = "name, id"

    name = fields.Char(required=True, readonly=True)
    external_id = fields.Char(
        string="Template ID",
        required=True,
        readonly=True,
        index=True,
        help="The template ID shown in the pdfs.build app.",
    )
    internal_id = fields.Char(readonly=True)
    description = fields.Text(readonly=True)
    status = fields.Selection(
        [("draft", "Draft"), ("published", "Published")],
        default="published",
        readonly=True,
        help="Only published templates can be rendered. A template that is no longer "
        "published on pdfs.build is marked Draft by the next sync.",
    )
    schema_json = fields.Text(string="JSON Schema", readonly=True)
    schema_html = fields.Html(compute="_compute_schema_html", sanitize=False, string="Fields")
    sample_data_json = fields.Text(string="Sample Data", readonly=True)
    data_fields = fields.Char(compute="_compute_data_fields", string="Data Fields")
    last_synced = fields.Datetime(readonly=True)
    preview_pdf = fields.Binary(string="Sample PDF", attachment=True, copy=False)
    active = fields.Boolean(
        default=True,
        help="Archive the templates you do not print from Odoo: they disappear from the "
        "pickers and the sync leaves them archived.",
    )
    report_ids = fields.One2many(
        "ir.actions.report", "pdfs_build_template_id", string="Reports"
    )
    report_count = fields.Integer(compute="_compute_report_count")

    _sql_constraints = [
        ("external_id_unique", "UNIQUE(external_id)", "This pdfs.build template is already synced."),
    ]

    @api.depends("schema_json")
    def _compute_data_fields(self):
        for template in self:
            properties = template._schema().get("properties") or {}
            summary = ", ".join(
                f"{name}[]" if (spec or {}).get("type") == "array" else name
                for name, spec in properties.items()
            )
            template.data_fields = summary if len(summary) <= 60 else summary[:57] + "..."

    @api.depends("schema_json")
    def _compute_schema_html(self):
        for template in self:
            template.schema_html = schema_html(template._schema())

    @api.depends("name", "status")
    def _compute_display_name(self):
        for template in self:
            suffix = "" if template.status == "published" else " (%s)" % _("draft")
            template.display_name = (template.name or "") + suffix

    @api.depends("report_ids")
    def _compute_report_count(self):
        for template in self:
            template.report_count = len(template.report_ids)

    def _schema(self):
        self.ensure_one()
        try:
            return json.loads(self.schema_json or "{}") or {}
        except ValueError:
            return {}

    def _sample_data(self):
        self.ensure_one()
        try:
            return json.loads(self.sample_data_json or "{}") or {}
        except ValueError as error:
            raise UserError(_("The sample data of %s is not valid JSON.", self.name)) from error

    # ---- pdfs.build API client (v2) ----

    @api.model
    def _api_settings(self):
        icp = self.env["ir.config_parameter"].sudo()
        return {
            # System parameter pdfs_build.api_url overrides the URL for staging tests.
            "url": (icp.get_param("pdfs_build.api_url") or DEFAULT_API_URL).strip().rstrip("/"),
            "api_key": (icp.get_param("pdfs_build.api_key") or "").strip(),
            "organization_id": (icp.get_param("pdfs_build.organization_id") or "").strip(),
        }

    @api.model
    def _api_request(self, method, path, settings=None, json_body=None, timeout=30):
        """Call the pdfs.build v2 API under the organization of the settings.

        Returns the PDF bytes or the decoded JSON body. Any failure becomes a
        UserError carrying the API's own error code, message and details.
        """
        settings = settings or self._api_settings()
        if not settings["api_key"] or not settings["organization_id"]:
            raise UserError(
                _(
                    "pdfs.build is not configured yet. Enter the API key and the "
                    "Organization ID under Settings > pdfs.build."
                )
            )
        url = "%s/v2/organizations/%s/%s" % (
            settings["url"],
            quote(settings["organization_id"], safe=""),
            path,
        )
        headers = {
            "Authorization": "Bearer %s" % settings["api_key"],
            "Accept": "application/pdf, image/png, application/json",
        }
        data = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body).encode()
        try:
            response = requests.request(method, url, headers=headers, data=data, timeout=timeout)
        except requests.RequestException as error:
            raise UserError(
                _("Could not reach pdfs.build at %(url)s: %(error)s", url=settings["url"], error=error)
            ) from error
        content_type = response.headers.get("Content-Type", "")
        if response.ok and not content_type.startswith("application/json"):
            return response.content  # a PDF, or a preview image
        try:
            body = response.json()
        except ValueError:
            body = {"error": (response.text or "")[:200] or "HTTP %s" % response.status_code}
        if not response.ok:
            raise UserError(self._api_error_message(response.status_code, body))
        return body

    @api.model
    def _status_hint(self, status):
        _ = self.env._
        return {
            401: _("Check the API key under Settings > pdfs.build."),
            402: _("Rendering over the API needs a pdfs.build Starter plan or higher."),
            403: _(
                "Check that the Organization ID under Settings > pdfs.build belongs to the "
                "API key, and that the template is published."
            ),
            404: _("The template is not published or its ID changed. Sync the templates."),
            429: _("The organization's monthly render quota is used up."),
        }.get(status, "")

    @api.model
    def _api_error_message(self, status, body):
        body = body if isinstance(body, dict) else {}
        lines = [_("pdfs.build answered %(status)s: %(error)s", status=status, error=body.get("error") or "")]
        if body.get("message"):
            lines.append(body["message"])
        for detail in body.get("details") or []:
            lines.append("- %s: %s" % (detail.get("path") or "/", detail.get("message", "")))
        for diagnostic in body.get("diagnostics") or []:
            where = "line %s: " % diagnostic["line"] if diagnostic.get("line") else ""
            lines.append("- %s%s" % (where, diagnostic.get("message", "")))
        hint = self._status_hint(status)
        if hint:
            lines.append(hint)
        return "\n".join(lines)

    # ---- Sync ----

    @api.model
    def _vals_from_api(self, template):
        return {
            "name": template.get("name") or template["externalId"],
            "external_id": template["externalId"],
            "internal_id": template.get("internalId") or "",
            "description": template.get("description") or "",
            "status": "published" if template.get("status") == "published" else "draft",
            "schema_json": json.dumps(template.get("schema") or {}, indent=2),
            "sample_data_json": json.dumps(template.get("sampleData") or {}, indent=2),
            "last_synced": fields.Datetime.now(),
        }

    def action_sync(self):
        """Pull the organization's published templates into Odoo.

        Works on any recordset: the list's header button passes the selection,
        which is irrelevant here.
        """
        listed = self._api_request("GET", "templates")
        if not isinstance(listed, list):
            raise UserError(_("Unexpected answer from pdfs.build when listing templates."))
        synced = self.browse()
        known = self.with_context(active_test=False)
        for summary in listed:
            external_id = summary.get("externalId")
            if not external_id:
                continue
            detail = self._api_request("GET", "templates/%s?version=latest" % quote(external_id, safe=""))
            vals = self._vals_from_api({**summary, **detail})
            template = known.search([("external_id", "=", external_id)], limit=1)
            if template:
                template.write(vals)
            else:
                template = self.create(vals)
            synced |= template
        (known.search([("status", "=", "published")]) - synced).write({"status": "draft"})
        synced.report_ids.filtered("pdfs_build_version")._pdfs_build_fetch_version_schema()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Templates synced"),
                "message": _(
                    "%(count)s published template(s). Archive the ones you do not print from Odoo.",
                    count=len(synced.filtered("active")),
                ),
            },
        }

    def action_refresh(self):
        for template in self:
            detail = self._api_request("GET", "templates/%s?version=latest" % quote(template.external_id, safe=""))
            template.write(self._vals_from_api(detail))
            template.report_ids.filtered("pdfs_build_version")._pdfs_build_fetch_version_schema()

    # ---- Buttons ----

    def action_preview_sample(self):
        """Render the sample data and show the PDF in a dialog."""
        self.ensure_one()
        pdf = self._api_request(
            "POST",
            "templates/%s/render" % quote(self.external_id, safe=""),
            json_body={"data": self._sample_data()},
            timeout=120,
        )
        self.preview_pdf = base64.b64encode(pdf)
        return preview_dialog(self, "pdfs_build.view_template_preview_form", _("%s (sample data)", self.name))

    def action_open_editor(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "%s/templates/%s" % (APP_URL, quote(self.internal_id or self.external_id, safe="")),
            "target": "new",
        }

    def action_new_report(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("New Report"),
            "res_model": "ir.actions.report",
            "views": [(self.env.ref("pdfs_build.view_report_form").id, "form")],
            "context": {"default_pdfs_build_template_id": self.id},
        }

    def action_use_for_existing_report(self):
        """Pick one of Odoo's own PDF reports (Invoices, Quotation / Order...) to render with this template."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Replace an existing report with %s", self.name),
            "res_model": "ir.actions.report",
            "views": [
                (self.env.ref("pdfs_build.view_report_pick_list").id, "list"),
                (self.env.ref("pdfs_build.view_report_form").id, "form"),
            ],
            "search_view_id": (self.env.ref("pdfs_build.view_report_pick_search").id, "pick"),
            "domain": [("report_type", "=", "qweb-pdf"), ("pdfs_build_template_id", "=", False)],
            "context": {"pdfs_build_template_id": self.id, "search_default_in_print_menu": 1},
        }

    def action_view_reports(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("pdfs_build.action_reports")
        action["domain"] = [("pdfs_build_template_id", "=", self.id)]
        action["context"] = {"default_pdfs_build_template_id": self.id}
        return action

    # ---- Data expression skeleton ----

    def _report_name(self):
        """A technical name for a new report on this template, unique among reports."""
        self.ensure_one()
        base = "pdfs_build.%s" % re.sub(r"[^a-z0-9_]+", "_", self.external_id.lower()).strip("_")
        Report = self.env["ir.actions.report"].sudo()
        name, counter = base, 1
        while Report.search_count([("report_name", "=", name)]):
            counter += 1
            name = "%s_%d" % (base, counter)
        return name

    def _data_expr_skeleton(self, model=None):
        """A Python dict literal with one entry per field of the template's schema.

        With ``model`` (a model name), every key that names a field of that model
        -- directly, through an alias, or nested through a many2one -- is written
        as the expression reading it, so a template whose schema follows Odoo's
        field names (see showcase/odoo/README.md in the pdfs.build repository)
        needs no editing at all. Other keys get a placeholder to fill in.
        """
        self.ensure_one()
        properties = self._schema().get("properties") or {}
        model = self.env[model] if model and model in self.env else None
        lines = ["{", *_skeleton_entries(properties, self._sample_data(), 1, model), "}"]
        return "\n".join(lines)


def preview_dialog(record, view_xmlid, title):
    """An action showing ``record`` in the given (PDF viewer) form view, as a large dialog."""
    return {
        "type": "ir.actions.act_window",
        "name": title,
        "res_model": record._name,
        "res_id": record.id,
        "views": [(record.env.ref(view_xmlid).id, "form")],
        "target": "new",
        "context": {"dialog_size": "extra-large"},
    }


# Schema keys follow Odoo 19; these are the same fields under their 17/18 names,
# plus the one slot the editor keeps at top level (a dotted alias walks many2ones).
FIELD_ALIASES = {
    "tax_ids": ("tax_id", "taxes_id"),
    "product_uom_id": ("product_uom",),
    "note": ("notes",),
    "logo": ("company_id.logo",),
}
# The loop variable of each nesting level of lines inside lines.
_LOOP_VARS = ("line", "item", "entry")
_X2MANY = ("one2many", "many2many")


def _resolve_field(model, name):
    """(dotted path, field) for ``name`` on ``model``, through the aliases; (None, None) if absent."""
    if model is None:
        return None, None
    for candidate in (name, *FIELD_ALIASES.get(name, ())):
        current, path = model, []
        for step in candidate.split("."):
            field = current._fields.get(step) if current is not None else None
            if field is None:
                break
            path.append(step)
            current = current.env[field.comodel_name] if field.type == "many2one" else None
        else:
            return ".".join(path), field
    return None, None


def _comodel(model, field):
    return model.env[field.comodel_name] if field is not None and field.comodel_name else None


def _scalar_expr(spec, field, model, expr, var):
    """The expression reading one scalar schema field, or None when the types cannot meet."""
    kind = spec.get("type")
    if kind == "string":
        if field.type == "binary":
            return "image(%s)" % expr
        if field.type == "html":
            return 'html2plaintext(%s or "")' % expr
        if field.type in ("date", "datetime"):
            return "format_date(%s)" % expr
        if field.type == "monetary":
            currency_field = getattr(field, "currency_field", None) or "currency_id"
            if currency_field not in model._fields:
                currency_field = "currency_id"
            return "format_amount(%s, %s.%s)" % (expr, var, currency_field)
        if field.type == "many2one":
            # A state's display_name is "Oregon (US)"; an address wants "Oregon".
            return "%s.%s" % (expr, "name" if field.comodel_name == "res.country.state" else "display_name")
        if field.type in _X2MANY:
            return '", ".join(%s.mapped("display_name"))' % expr
        if field.type in ("float", "integer"):
            # A string-typed number on a document with a currency is a price
            # (price_unit is a plain Float in Odoo); elsewhere it is just text.
            if "currency_id" in model._fields:
                return "format_amount(%s, %s.currency_id)" % (expr, var)
            return "str(%s)" % expr
        return expr
    if kind in ("number", "integer"):
        return expr if field.type in ("float", "integer", "monetary") else None
    if kind == "boolean":
        return expr if field.type == "boolean" else None
    return None


def _skeleton_entries(properties, sample, depth, model=None, var="record"):
    pad = "    " * depth
    sample = sample if isinstance(sample, dict) else {}
    lines = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        # One line, so a multi-line description cannot leak out of the comment.
        description = " ".join((spec.get("description") or spec.get("title") or "").split())
        if description:
            lines.append("%s# %s" % (pad, description))
        value, comment = _skeleton_value(name, spec, sample.get(name), depth, model, var)
        lines.append("%s%s: %s,%s" % (pad, json.dumps(name), value, "  # " + comment if comment else ""))
    return lines


def _skeleton_value(name, spec, sample, depth, model, var):
    """(python source, trailing comment) for one schema field: the expression reading the
    matching Odoo field when there is one, else a placeholder."""
    pad = "    " * depth
    kind = spec.get("type")
    path, field = _resolve_field(model, name)
    expr = "%s.%s" % (var, path) if path else None
    todo = "TODO" if model is not None else ""
    if kind == "array":
        items = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        if name == "tax_totals" and field is not None and field.type not in _X2MANY:
            return "tax_groups(%s)" % var, ""
        if items.get("type") == "object" and items.get("properties"):
            first = sample[0] if isinstance(sample, list) and sample else {}
            related = _comodel(model, field) if field is not None and field.type in _X2MANY else None
            loop_var = _LOOP_VARS[min(depth // 2, len(_LOOP_VARS) - 1)]
            inner = "\n".join(_skeleton_entries(items["properties"], first, depth + 2, related, loop_var))
            source = expr if related is not None else LINES_PLACEHOLDER
            # Sections and notes are not lines. (account.move.line sets display_type
            # on product lines too, so the test is for the two kinds, not for truthiness.)
            guard = (
                '\n%s    if %s.display_type not in ("line_section", "line_note")' % (pad, loop_var)
                if related is not None and "display_type" in related._fields
                else ""
            )
            return (
                "[\n%s    {\n%s\n%s    }\n%s    for %s in %s%s\n%s]"
                % (pad, inner, pad, pad, loop_var, source, guard, pad),
                "",
            )
        if field is not None and field.type in _X2MANY:
            return '%s.mapped("display_name")' % expr, ""
        return "[]", todo
    if kind == "object" and spec.get("properties"):
        related = _comodel(model, field) if field is not None and field.type == "many2one" else None
        inner_var = expr if related is not None else var
        inner = "\n".join(_skeleton_entries(spec["properties"], sample, depth + 1, related, inner_var))
        return "{\n%s\n%s}" % (inner, pad), ""
    if kind == "string" and spec.get("format") == "image":
        if spec.get("x-image-mode") == "static":
            return json.dumps(sample or ""), "image stored with the template"
        if field is not None and field.type == "binary":
            return "image(%s)" % expr, ""
        return "image(record.image_1920)", "TODO: an image or binary field"
    if field is not None:
        resolved = _scalar_expr(spec, field, model, expr, var)
        if resolved is not None:
            return resolved, ""
    if name == "weight_uom_name" and kind == "string" and model is not None:
        return "weight_uom_name", ""
    example = "e.g. %s" % json.dumps(sample, ensure_ascii=False)[:60] if sample not in (None, "", [], {}) else ""
    comment = ": ".join(part for part in (todo, example) if part)
    if kind in ("number", "integer"):
        return "0", comment
    if kind == "boolean":
        return "False", comment
    return '""', comment


def _schema_rows(properties, required, prefix=""):
    """(path, type, required, description) for every field, nested ones included."""
    rows = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("type") or "any"
        items = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        if kind == "string" and spec.get("format") == "image":
            kind = "image (stored with the template)" if spec.get("x-image-mode") == "static" else "image"
        elif kind == "array":
            kind = "list of %s" % (items.get("type") or "any")
        rows.append((prefix + name, kind, name in required, spec.get("description") or spec.get("title") or ""))
        if spec.get("type") == "object" and spec.get("properties"):
            rows += _schema_rows(spec["properties"], spec.get("required") or [], prefix + name + ".")
        elif items.get("type") == "object" and items.get("properties"):
            rows += _schema_rows(items["properties"], items.get("required") or [], prefix + name + "[].")
    return rows


def schema_html(schema):
    """The fields of a JSON Schema as an HTML table, or "" for an empty schema."""
    schema = schema if isinstance(schema, dict) else {}
    rows = _schema_rows(schema.get("properties") or {}, schema.get("required") or [])
    return _schema_table(rows) if rows else ""


def _schema_table(rows):
    body = "".join(
        "<tr><td><code>%s</code></td><td>%s</td><td class=\"text-center\">%s</td><td>%s</td></tr>"
        % (escape(path), escape(kind), "&#10003;" if required else "", escape(description))
        for path, kind, required, description in rows
    )
    return Markup(
        '<table class="table table-sm"><thead><tr><th>Field</th><th>Type</th>'
        '<th class="text-center">Required</th><th>Description</th></tr></thead><tbody>%s</tbody></table>'
    ) % Markup(body)
