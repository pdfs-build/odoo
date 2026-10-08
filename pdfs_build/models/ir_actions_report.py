import base64
import binascii
import json
import re
from collections import OrderedDict
from datetime import date
from decimal import Decimal
from urllib.parse import quote

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import html2plaintext
from odoo.tools.mimetypes import guess_mimetype
from odoo.tools.misc import format_amount, format_date, format_datetime
from odoo.tools.pdf import merge_pdf
from odoo.tools.safe_eval import datetime as safe_datetime
from odoo.tools.safe_eval import dateutil as safe_dateutil
from odoo.tools.safe_eval import safe_eval
from odoo.tools.safe_eval import time as safe_time

from .pdfs_build_template import LINES_PLACEHOLDER, preview_dialog, schema_html

_EMPTY = {"string": "", "number": 0, "integer": 0, "array": [], "object": {}}
_ARTICLE_ID = re.compile(r'data-oe-id="(\d+)"')
# Chatter, activity and access-control plumbing: never what a template needs.
_NOISE_PREFIXES = ("message_", "activity_", "website_message_", "rating_", "my_activity_", "has_message", "access_")
_PREFERRED_LINES = ("order_line", "invoice_line_ids", "line_ids", "move_line_ids")


def _lines_field(names):
    """The one2many field that most likely holds the document's lines, or None."""
    names = list(names)
    for preferred in _PREFERRED_LINES:
        if preferred in names:
            return preferred
    return next((name for name in sorted(names) if "line" in name), None)


def image(value):
    """A binary field value as the data URL that pdfs.build image fields expect."""
    if not value:
        return ""
    if isinstance(value, str):
        value = value.encode()
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raw = value
    return "data:%s;base64,%s" % (guess_mimetype(raw, default="image/png"), base64.b64encode(raw).decode())


def tax_groups(record):
    """The document's taxes as [{"name", "base", "amount"}] with formatted amounts.

    Read from Odoo's ``tax_totals`` dictionary (invoices, sales and purchase
    orders), which changed shape in Odoo 18; both shapes are handled. Empty for
    a record without taxes or without the field.
    """
    totals = record.tax_totals if "tax_totals" in record._fields else None
    if not isinstance(totals, dict):
        return []
    currency = record.currency_id if "currency_id" in record._fields else record.env.company.currency_id

    def money(amount):
        return format_amount(record.env, amount or 0.0, currency)

    groups = []
    by_subtotal = totals.get("groups_by_subtotal") or totals.get("groups_by_subtotals")
    if isinstance(by_subtotal, dict):  # Odoo 17
        for subtotal in by_subtotal.values():
            for group in subtotal or ():
                groups.append({
                    "name": group.get("tax_group_name") or "",
                    "base": group.get("formatted_tax_group_base_amount") or money(group.get("tax_group_base_amount")),
                    "amount": group.get("formatted_tax_group_amount") or money(group.get("tax_group_amount")),
                })
        return groups
    for subtotal in totals.get("subtotals") or ():  # Odoo 18+
        for group in subtotal.get("tax_groups") or ():
            groups.append({
                "name": group.get("group_label") or group.get("group_name") or "",
                "base": money(group.get("base_amount_currency", group.get("base_amount"))),
                "amount": money(group.get("tax_amount_currency", group.get("tax_amount"))),
            })
    return groups


def _coerce(value, schema):
    """Make Odoo values fit the template's JSON Schema.

    An empty Odoo field is False: it becomes "", 0, [] or {} according to the
    declared type. Dates become ISO strings; decimals become floats. Recordsets
    are refused with a hint, since the template cannot render them.
    """
    schema = schema if isinstance(schema, dict) else {}
    kind = schema.get("type")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        return {key: _coerce(item, properties.get(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(item, schema.get("items")) for item in value]
    if (value is False or value is None) and kind in _EMPTY:
        return _EMPTY[kind]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, models.BaseModel):
        raise TypeError(value)
    return value


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    model_id = fields.Many2one(inverse="_inverse_model_id")
    pdfs_build_template_id = fields.Many2one(
        "pdfs_build.template",
        string="pdfs.build Template",
        ondelete="restrict",
        index=True,
        help="When set, this report is rendered by pdfs.build instead of QWeb.",
    )
    pdfs_build_data_expr = fields.Text(
        string="Data Expression",
        help="Python expression evaluated for each printed record. It must return the "
        "dict the template expects. Available: record, env, user, company, datetime, "
        "dateutil, relativedelta, time, image(binary), html2plaintext(html), "
        "format_date(value), format_datetime(value), format_amount(amount, currency), "
        "tax_groups(record), weight_uom_name.",
    )
    pdfs_build_version = fields.Char(
        string="Template Version",
        size=64,
        help="Empty renders the latest published version. Otherwise a version number "
        "(4), 'draft', or a channel name such as 'staging'.",
    )
    pdfs_build_preview = fields.Text(string="Data Preview", readonly=True, copy=False)
    pdfs_build_preview_pdf = fields.Binary(string="Preview PDF", attachment=True, copy=False)
    pdfs_build_preview_record_id = fields.Many2oneReference(
        string="Preview Record",
        model_field="model",
        copy=False,
        help="The record Preview Data and Preview PDF use. Empty: the latest record of the model.",
    )
    pdfs_build_schema_json = fields.Text(
        string="Version Schema",
        readonly=True,
        copy=False,
        help="The schema of the pinned Template Version, fetched from pdfs.build. "
        "Empty when the report renders the latest version: the template's schema applies.",
    )
    pdfs_build_schema_html = fields.Html(compute="_compute_pdfs_build_schema_html", string="Template Fields")
    pdfs_build_model_field_ids = fields.Many2many(
        "ir.model.fields",
        compute="_compute_pdfs_build_model_field_ids",
        string="Odoo Fields",
        help="The fields of the model, to use as record.<field> in the data expression.",
    )

    @api.depends("pdfs_build_schema_json", "pdfs_build_template_id.schema_html")
    def _compute_pdfs_build_schema_html(self):
        for report in self:
            if report.pdfs_build_schema_json:
                report.pdfs_build_schema_html = schema_html(report._pdfs_build_schema())
            else:
                report.pdfs_build_schema_html = report.pdfs_build_template_id.schema_html

    def _pdfs_build_schema(self):
        """The schema the report's renders are validated against: the pinned version's, else the template's."""
        self.ensure_one()
        if self.pdfs_build_schema_json:
            try:
                return json.loads(self.pdfs_build_schema_json) or {}
            except ValueError:
                return {}
        return self.pdfs_build_template_id._schema() if self.pdfs_build_template_id else {}

    def _pdfs_build_fetch_version_schema(self):
        """Keep the pinned version's schema on the report, so data is fitted to what renders."""
        for report in self:
            version = (report.pdfs_build_version or "").strip()
            template = report.pdfs_build_template_id
            if not version or not template:
                report.pdfs_build_schema_json = False
                continue
            detail = template._api_request(
                "GET", "templates/%s?version=%s" % (quote(template.external_id, safe=""), quote(version, safe=""))
            )
            report.pdfs_build_schema_json = json.dumps(detail.get("schema") or {}, indent=2)

    @api.model_create_multi
    def create(self, vals_list):
        # A report created from code (RPC, a script) gets the same prefill as the
        # form: technical name, file name and the expression for its model.
        for vals in vals_list:
            template = self.env["pdfs_build.template"].browse(vals.get("pdfs_build_template_id") or [])
            if template.exists():
                draft = self.new(vals)
                if draft.model_id and not draft.model:
                    draft.model = draft.model_id.model
                draft._pdfs_build_prefill(template)
                for name in ("report_type", "name", "report_name", "print_report_name", "pdfs_build_data_expr"):
                    if not vals.get(name):
                        vals[name] = draft[name]
        reports = super().create(vals_list)
        # A pdfs.build report exists to be printed: put it in the Print menu right away.
        reports.filtered(lambda r: r.pdfs_build_template_id and not r.binding_model_id).create_action()
        reports.filtered("pdfs_build_version")._pdfs_build_fetch_version_schema()
        return reports

    def write(self, vals):
        previous = {report.id: report.model for report in self} if "model" in vals or "model_id" in vals else None
        result = super().write(vals)
        if previous is not None and "pdfs_build_data_expr" not in vals:
            self._pdfs_build_regenerate_expr(previous)
        if "pdfs_build_version" in vals or "pdfs_build_template_id" in vals:
            self._pdfs_build_fetch_version_schema()
        return result

    @api.depends("model")
    def _compute_pdfs_build_model_field_ids(self):
        Fields = self.env["ir.model.fields"]
        for report in self:
            fields_ = Fields.search([("model", "=", report.model)], order="field_description") if report.model else Fields
            report.pdfs_build_model_field_ids = fields_.filtered(
                lambda f: not f.name.startswith(_NOISE_PREFIXES)
            )

    def _pdfs_build_lines_field(self):
        self.ensure_one()
        if not self.model or self.model not in self.env:
            return None
        model_fields = self.env[self.model]._fields
        return _lines_field(name for name, field in model_fields.items() if field.type == "one2many")

    def _pdfs_build_fill_lines_field(self):
        """Replace the skeleton's lines placeholder with the model's lines field, when known."""
        expression = self.pdfs_build_data_expr or ""
        lines_field = LINES_PLACEHOLDER in expression and self._pdfs_build_lines_field()
        if lines_field:
            self.pdfs_build_data_expr = expression.replace(LINES_PLACEHOLDER, "record.%s" % lines_field)

    def _inverse_model_id(self):
        for report in self.filtered("model_id"):
            report.model = report.model_id.model

    @api.onchange("model_id")
    def _onchange_model_id(self):
        if self.model_id:
            # The model the form held before this change: the in-memory value for a
            # record being created, the stored one otherwise.
            previous = self.model or (self._origin.model if self._origin else False)
            self.model = self.model_id.model
            self.pdfs_build_preview_record_id = False
            self._pdfs_build_regenerate_expr(previous)

    @api.onchange("pdfs_build_template_id")
    def _onchange_pdfs_build_template_id(self):
        if self.pdfs_build_template_id:
            self._pdfs_build_prefill(self.pdfs_build_template_id)

    def _pdfs_build_prefill(self, template):
        """Point the report at ``template`` and fill in whatever is still empty."""
        self.pdfs_build_template_id = template
        self.report_type = "qweb-pdf"
        if not self.name:
            self.name = template.name
        if not self.report_name:
            self.report_name = template._report_name()
        if not self.print_report_name:
            # A Python literal, so names with quotes, % or backslashes stay valid.
            self.print_report_name = "object.display_name + ' - ' + %r" % template.name
        if not self.pdfs_build_data_expr:
            self.pdfs_build_data_expr = template._data_expr_skeleton(self.model)
            self._pdfs_build_fill_lines_field()

    def _pdfs_build_expr_untouched(self, previous_model):
        """True when the expression is still the generated skeleton (for ``previous_model``
        or for no model), so it can be regenerated for another model without losing edits."""
        self.ensure_one()
        template = self.pdfs_build_template_id
        expression = (self.pdfs_build_data_expr or "").strip()
        if not template or not expression:
            return True
        # Both skeletons, before and after the lines placeholder was filled in, for the
        # model the expression was generated for and for no model at all.
        candidates = set()
        for model in {None, previous_model or None}:
            skeleton = template._data_expr_skeleton(model)
            candidates.add(skeleton)
            lines_field = model and model in self.env and _lines_field(
                name for name, field in self.env[model]._fields.items() if field.type == "one2many"
            )
            if lines_field:
                candidates.add(skeleton.replace(LINES_PLACEHOLDER, "record.%s" % lines_field))
        return expression in candidates

    def _pdfs_build_regenerate_expr(self, previous_model):
        """Rewrite a still-untouched expression for the report's current model."""
        for report in self.filtered("pdfs_build_template_id"):
            if report._pdfs_build_expr_untouched(previous_model.get(report.id) if isinstance(previous_model, dict) else previous_model):
                report.pdfs_build_data_expr = report.pdfs_build_template_id._data_expr_skeleton(report.model)
                report._pdfs_build_fill_lines_field()

    def action_pdfs_build_use_template(self):
        """List header button: render the selected (existing) reports with the template in context."""
        template = self.env["pdfs_build.template"].browse(self.env.context.get("pdfs_build_template_id"))
        if not template.exists():
            raise UserError(_("Open this list from a template's 'Use for Existing Report' button."))
        for report in self:
            report._pdfs_build_prefill(template)
        return {
            "type": "ir.actions.act_window",
            "res_model": "ir.actions.report",
            "res_id": self[:1].id,
            "views": [(self.env.ref("pdfs_build.view_report_form").id, "form")],
        }

    @api.constrains("report_name", "pdfs_build_template_id")
    def _check_pdfs_build_report_name(self):
        for report in self.filtered("pdfs_build_template_id"):
            if self.search_count([("report_name", "=", report.report_name), ("id", "!=", report.id)]):
                raise ValidationError(
                    _("Another report already uses the technical name %s. Choose another one.", report.report_name)
                )

    # ---- Data ----

    def _pdfs_build_eval_context(self, record):
        env = self.env
        return {
            "record": record,
            "object": record,
            "env": env,
            "user": env.user,
            "company": env.company,
            "datetime": safe_datetime,
            "dateutil": safe_dateutil,
            "relativedelta": relativedelta,
            "time": safe_time,
            "image": image,
            "html2plaintext": html2plaintext,
            "format_date": lambda value, date_format=False: format_date(env, value, date_format=date_format),
            "format_datetime": lambda value, dt_format=False: format_datetime(env, value, dt_format=dt_format),
            "format_amount": lambda amount, currency: format_amount(env, amount, currency),
            "tax_groups": tax_groups,
            # Odoo's weight unit is a setting, not a field: "kg" or "lb".
            "weight_uom_name": (
                env["product.template"]._get_weight_uom_name_from_ir_config_parameter()
                if "product.template" in env
                else ""
            ),
        }

    def _pdfs_build_data(self, record, schema=None):
        """The template data for one record: the expression's dict, fitted to ``schema``
        (the pinned version's or the template's when not given)."""
        self.ensure_one()
        expression = (self.pdfs_build_data_expr or "").strip()
        if not expression:
            raise UserError(
                _("The report %s has no data expression. Fill it in under pdfs.build > Reports.", self.name)
            )
        try:
            data = safe_eval(expression, self._pdfs_build_eval_context(record))
        except UserError:
            raise
        except Exception as error:
            raise UserError(
                _(
                    "The data expression of the report %(report)s failed for %(record)s:\n%(error)s",
                    report=self.name,
                    record=record.display_name,
                    error=error,
                )
            ) from error
        if not isinstance(data, dict):
            raise UserError(
                _("The data expression of the report %s must return a dict, the JSON object the template expects.", self.name)
            )
        try:
            return _coerce(data, self._pdfs_build_schema() if schema is None else schema)
        except TypeError as error:
            raise UserError(
                _(
                    "The data expression returns the record(s) %s. Send one of their fields "
                    "instead (for example .name), or a list comprehension for lines.",
                    error,
                )
            ) from error

    def _pdfs_build_render_contract(self):
        """(schema, version) a render of this report validates against and compiles.

        A version number is frozen: its schema was stored when the pin was set.
        A channel moves when a version is promoted, so it is resolved now and the
        exact version it points at is used, with that version's schema. Preview
        Data and PDF rendering both go through here, so they cannot disagree.
        """
        self.ensure_one()
        template = self.pdfs_build_template_id
        selector = (self.pdfs_build_version or "").strip() or "latest"
        if selector.isdigit():
            return self._pdfs_build_schema(), int(selector)
        detail = template._api_request(
            "GET", "templates/%s?version=%s" % (quote(template.external_id, safe=""), quote(selector, safe=""))
        )
        return detail.get("schema") or {}, detail.get("version") or selector

    def _pdfs_build_render(self, record):
        self.ensure_one()
        template = self.pdfs_build_template_id
        schema, version = self._pdfs_build_render_contract()
        return template._api_request(
            "POST",
            "templates/%s/render" % quote(template.external_id, safe=""),
            json_body={"data": self._pdfs_build_data(record, schema), "version": version},
            timeout=120,
        )

    # Odoo's PDF pipeline is one inheritance chain on _render_qweb_pdf_prepare_streams:
    # other modules (the quotation document builder, e-invoice XML embedding...)
    # call super() first and post-process the streams that come back, and base
    # ends the chain by rendering HTML and calling wkhtmltopdf. The position of
    # this module in that chain depends on the module load order, so instead of
    # short-circuiting the chain this module lets all of it run and replaces only
    # the two leaf steps: the HTML stands in with the record markers Odoo splits
    # on, and _run_wkhtmltopdf renders through pdfs.build. Attachments, duplicate
    # ids, per-record splitting and every other module's post-processing then
    # behave exactly as they do for Odoo's own reports.

    def _render_qweb_pdf_prepare_streams(self, report_ref, data, res_ids=None):
        report = self._get_report(report_ref)
        if not report.pdfs_build_template_id:
            return super()._render_qweb_pdf_prepare_streams(report_ref, data, res_ids=res_ids)
        res_ids = list(res_ids or [])
        if not res_ids:
            raise UserError(_("Select at least one record to print with %s.", report.name))
        chain = super(IrActionsReport, self.with_context(pdfs_build_render=True))._render_qweb_pdf_prepare_streams
        if len(res_ids) != len(set(res_ids)):
            # Odoo prints duplicated ids as one combined document, in a single pass.
            return chain(report_ref, data, res_ids=res_ids)
        # One pass per record: the pipeline then handles one document at a time,
        # which keeps multi-page documents whole (Odoo's splitter needs the PDF
        # outlines wkhtmltopdf writes) and saves each record's attachment.
        streams = OrderedDict()
        for res_id in res_ids:
            streams.update(chain(report_ref, data, res_ids=[res_id]))
        return streams

    def get_wkhtmltopdf_state(self):
        # pdfs.build renders the PDF: do not let the pipeline refuse on a host without wkhtmltopdf.
        if self.env.context.get("pdfs_build_render"):
            return "ok"
        return super().get_wkhtmltopdf_state()

    @api.model
    def _render_qweb_html(self, report_ref, docids, data=None):
        report = self._get_report(report_ref)
        if not report.pdfs_build_template_id:
            return super()._render_qweb_html(report_ref, docids, data=data)
        # There is no QWeb view behind a pdfs.build report. The articles carry the
        # markers Odoo's pipeline uses to split and save the PDF per record, and
        # tell _run_wkhtmltopdf which records to render.
        articles = Markup("").join(
            Markup('<div class="article" data-oe-model="%s" data-oe-id="%d"></div>') % (report.model, res_id)
            for res_id in (docids or [])
        )
        note = _("%s is rendered by pdfs.build. Print it to get the PDF.", report.name)
        return Markup("<html><body><main>%s<p>%s</p></main></body></html>") % (articles, note), "html"

    @api.model
    def _run_wkhtmltopdf(
        self,
        bodies,
        report_ref=False,
        header=None,
        footer=None,
        landscape=False,
        specific_paperformat_args=None,
        set_viewport_size=False,
    ):
        report = self._get_report(report_ref) if report_ref else self.env["ir.actions.report"]
        if not report.pdfs_build_template_id:
            return super()._run_wkhtmltopdf(
                bodies,
                report_ref=report_ref,
                header=header,
                footer=footer,
                landscape=landscape,
                specific_paperformat_args=specific_paperformat_args,
                set_viewport_size=set_viewport_size,
            )
        # One body per article, in print order (several only when Odoo prints
        # duplicated ids as one document); a record printed twice is rendered once.
        res_ids = [int(match) for body in bodies for match in _ARTICLE_ID.findall(str(body))]
        if not res_ids:
            raise UserError(_("Select at least one record to print with %s.", report.name))
        rendered = {}
        for record in self.env[report.model].browse(list(dict.fromkeys(res_ids))):
            rendered[record.id] = report._pdfs_build_render(record)
        pdfs = [rendered[res_id] for res_id in res_ids]
        return pdfs[0] if len(pdfs) == 1 else merge_pdf(pdfs)

    # ---- Buttons ----

    def _pdfs_build_preview_record(self):
        """The chosen preview record, else the latest record of the model."""
        self.ensure_one()
        Model = self.env[self.model]
        record = Model.browse(self.pdfs_build_preview_record_id or []).exists()
        if not record:
            record = Model.search([], order="id desc", limit=1)
        if not record:
            raise UserError(_("There is no %s record yet to try the report with.", self.model))
        return record

    def action_pdfs_build_preview(self):
        self.ensure_one()
        record = self._pdfs_build_preview_record()
        schema, version = self._pdfs_build_render_contract()
        data = self._pdfs_build_data(record, schema)
        self.pdfs_build_preview = "// %s\n%s" % (
            _("Data for %(record)s, template version %(version)s", record=record.display_name, version=version),
            json.dumps(data, indent=2, ensure_ascii=False),
        )

    def action_pdfs_build_preview_pdf(self):
        """Render the latest record through pdfs.build and show the PDF in a dialog."""
        self.ensure_one()
        record = self._pdfs_build_preview_record()
        self.pdfs_build_preview_pdf = base64.b64encode(self._pdfs_build_render(record))
        return preview_dialog(self, "pdfs_build.view_report_preview_form", _("%s: %s", self.name, record.display_name))
