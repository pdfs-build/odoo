import base64
import io
import json
from datetime import date
from unittest.mock import Mock, patch

import requests

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase
from odoo.tools.misc import format_amount
from odoo.tools.pdf import PdfFileReader, PdfFileWriter

from odoo.addons.pdfs_build.models.ir_actions_report import tax_groups
from odoo.addons.pdfs_build.models.pdfs_build_template import LINES_PLACEHOLDER

REQUEST = "odoo.addons.pdfs_build.models.pdfs_build_template.requests.request"
ORG = "org_test"
BASE = "https://api.pdfs.build/v2/organizations/%s" % ORG
SCHEMA = {
    "type": "object",
    "required": ["clientName", "items"],
    "properties": {
        "clientName": {"type": "string", "description": "Client company name"},
        "clientEmail": {"type": "string"},
        "total": {"type": "number"},
        "paid": {"type": "boolean"},
        "logo": {"type": "string", "format": "image"},
        "brand": {"type": "string", "format": "image", "x-image-mode": "static"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "amount": {"type": "number"}},
            },
        },
    },
}
SAMPLE = {
    "clientName": "Acme Corp",
    "brand": "templates/x/images/brand.png",
    "items": [{"name": "Setup", "amount": 450}],
}
LISTED = {
    "externalId": "odoo-quotation",
    "internalId": "11111111-2222-3333-4444-555555555555",
    "name": "Quotation",
    "description": None,
    "status": "published",
}
DETAIL = {**LISTED, "schema": SCHEMA, "sampleData": SAMPLE, "schemaLocked": False}


def response(status=200, body=None, pdf=None):
    result = Mock(status_code=status, ok=status < 400)
    if pdf is not None:
        result.headers = {"Content-Type": "application/pdf"}
        result.content = pdf
        result.json.side_effect = ValueError("not json")
        result.text = ""
    else:
        result.headers = {"Content-Type": "application/json"}
        result.content = json.dumps(body).encode()
        result.text = result.content.decode()
        result.json.return_value = body
    return result


def blank_pdf(pages=1):
    writer = PdfFileWriter()
    add_blank_page = getattr(writer, "add_blank_page", None) or writer.addBlankPage  # PyPDF2 1.x on Odoo 17
    for _ in range(pages):
        add_blank_page(width=200, height=200)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def api(pdfs=(), detail=DETAIL, render=None, version=None):
    """A requests.request stand-in: template GETs answer ``detail`` (with ``version``
    when given), renders answer ``render`` or the next of ``pdfs`` in turn."""
    pdfs = list(pdfs)

    def fake(method, url, **kwargs):
        if method == "GET" and "/templates/odoo-quotation" in url:
            return response(200, {**detail, "version": version})
        if method == "POST" and url.endswith("/render"):
            return render or response(200, pdf=pdfs.pop(0))
        return response(404, {"error": "Template not found"})

    return fake


def posted(request):
    """The render bodies sent through a patched requests.request, in order."""
    return [json.loads(c.kwargs["data"]) for c in request.call_args_list if c.args[0] == "POST"]


class TestPdfsBuild(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        icp = cls.env["ir.config_parameter"].sudo()
        icp.set_param("pdfs_build.api_key", "prs_test")
        icp.set_param("pdfs_build.organization_id", ORG)
        cls.Template = cls.env["pdfs_build.template"]
        cls.Report = cls.env["ir.actions.report"]
        cls.partner = cls.env["res.partner"].create({"name": "Acme Corp", "email": "john@acme.test"})

    def _sync(self, listed=(LISTED,), detail=DETAIL):
        def fake(method, url, **kwargs):
            if url == BASE + "/templates":
                return response(200, list(listed))
            if url == BASE + "/templates/odoo-quotation?version=latest":
                return response(200, detail)
            return response(404, {"error": "Template not found"})

        with patch(REQUEST, side_effect=fake) as request:
            self.Template.action_sync()
        return request

    def _report(self, expression, **vals):
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        return self.Report.create(
            {
                "name": "Quotation",
                "model": "res.partner",
                "report_type": "qweb-pdf",
                "report_name": "pdfs_build.odoo_quotation",
                "pdfs_build_template_id": template.id,
                "pdfs_build_data_expr": expression,
                **vals,
            }
        )

    def test_sync_creates_updates_and_unpublishes(self):
        request = self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        # The list's header button calls it on the selected rows.
        with patch(REQUEST, return_value=response(200, [])):
            template.action_sync()
        self.assertEqual(template.status, "draft")
        request = self._sync()
        self.assertEqual(len(template), 1)
        self.assertEqual(template.name, "Quotation")
        self.assertEqual(template.status, "published")
        self.assertEqual(template.data_fields, "clientName, clientEmail, total, paid, logo, brand, items[]")
        self.assertEqual(json.loads(template.sample_data_json), SAMPLE)
        self.assertEqual(request.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer prs_test")

        self._sync(detail={**DETAIL, "name": "Quotation v2"})
        self.assertEqual(template.name, "Quotation v2")
        self.assertEqual(self.Template.search_count([]), 1)

        self._sync(listed=())
        self.assertEqual(template.status, "draft", "no longer listed as published")

    def test_archived_templates_stay_archived_and_hidden(self):
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        template.action_archive()
        self._sync(detail={**DETAIL, "name": "Renamed"})
        self.assertEqual(self.Template.search_count([]), 0, "archived: out of the default lists")
        archived = self.Template.with_context(active_test=False).search([])
        self.assertEqual(len(archived), 1, "not duplicated by the sync")
        self.assertFalse(archived.active)
        self.assertEqual(archived.name, "Renamed", "still kept up to date")
        self._sync(listed=())
        self.assertEqual(archived.status, "draft")

    def test_connection_errors_are_user_errors(self):
        with patch(REQUEST, side_effect=requests.ConnectionError("no route")):
            with self.assertRaisesRegex(UserError, "Could not reach pdfs.build"):
                self.Template.action_sync()
        with patch(REQUEST, return_value=response(401, {"error": "Unauthorized"})):
            with self.assertRaisesRegex(UserError, "Check the API key"):
                self.Template.action_sync()
        self.env["ir.config_parameter"].sudo().set_param("pdfs_build.api_key", "")
        with self.assertRaisesRegex(UserError, "not configured"):
            self.Template.action_sync()

    ODOO_SCHEMA = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "email": {"type": "string"},
            "comment": {"type": "string"},
            "create_date": {"type": "string"},
            "color": {"type": "number"},
            "active": {"type": "boolean"},
            "category_id": {"type": "string"},
            "parent_id": {"type": "object", "properties": {"name": {"type": "string"}, "city": {"type": "string"}}},
            "company_id": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "primary_color": {"type": "string"}},
            },
            "logo": {"type": "string", "format": "image"},
            "child_ids": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "email": {"type": "string"},
                        "bank_ids": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"acc_number": {"type": "string"}, "bank_id": {"type": "string"}},
                            },
                        },
                    },
                },
            },
        },
    }

    def test_skeleton_maps_odoo_fields(self):
        """A schema named after Odoo's fields is mapped without a placeholder left behind."""
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        extra = {
            "tax_totals": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
            "nickname": {"type": "string"},
            "weight_uom_name": {"type": "string"},
            "state_id": {"type": "string"},
        }
        template.schema_json = json.dumps({**self.ODOO_SCHEMA, "properties": {**self.ODOO_SCHEMA["properties"], **extra}})
        template.sample_data_json = json.dumps({"nickname": "Ace"})
        skeleton = template._data_expr_skeleton("res.partner")
        compile(skeleton, "<skeleton>", "eval")
        for expected in (
            '"name": record.name,',
            '"comment": html2plaintext(record.comment or ""),',
            '"create_date": format_date(record.create_date),',
            '"color": record.color,',
            '"active": record.active,',
            '"category_id": ", ".join(record.category_id.mapped("display_name")),',
            '"city": record.parent_id.city,',
            '"primary_color": record.company_id.primary_color,',
            '"logo": image(record.company_id.logo),',
            "for line in record.child_ids",
            '"email": line.email,',
            "for item in line.bank_ids",
            '"bank_id": item.bank_id.display_name,',
            '"nickname": "",  # TODO: e.g. "Ace"',
            '"weight_uom_name": weight_uom_name,',
            '"state_id": record.state_id.name,',
            "for line in %s" % LINES_PLACEHOLDER,
        ):
            self.assertIn(expected, skeleton)
        # The model-less skeleton is unchanged by the mapping.
        self.assertIn('"nickname": "",  # e.g. "Ace"', template._data_expr_skeleton())

    def test_mapped_expression_evaluates(self):
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        template.schema_json = json.dumps(self.ODOO_SCHEMA)
        template.sample_data_json = "{}"
        partner = self.env["res.partner"].create({
            "name": "Toyco Inc.",
            "email": "accounts@toyco.com",
            "company_id": self.env.company.id,
            "comment": "<p>Net <b>30</b></p>",
            "child_ids": [(0, 0, {"name": "Dana Whitfield", "email": "dana@toyco.com"})],
        })
        report = self.env["ir.actions.report"].create({
            "name": "Partner card",
            "model": "res.partner",
            "pdfs_build_template_id": template.id,
        })
        self.assertEqual(report.pdfs_build_data_expr, template._data_expr_skeleton("res.partner"))
        data = report._pdfs_build_data(partner)
        self.assertEqual(data["name"], "Toyco Inc.")
        self.assertEqual(data["comment"], "Net *30*")  # html2plaintext marks bold with stars
        self.assertEqual(data["parent_id"], {"name": "", "city": ""})
        self.assertEqual(data["child_ids"], [{"name": "Dana Whitfield", "email": "dana@toyco.com", "bank_ids": []}])
        self.assertEqual(data["company_id"]["name"], self.env.company.name)
        self.assertTrue(data["logo"].startswith("data:image/"))
        self.assertIsInstance(data["active"], bool)
        self.assertEqual(report._pdfs_build_eval_context(partner)["weight_uom_name"], "")

    def test_expression_follows_the_model_until_edited(self):
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        template.schema_json = json.dumps(self.ODOO_SCHEMA)
        Report = self.env["ir.actions.report"]
        report = Report.create({"name": "Card", "model": "res.bank", "pdfs_build_template_id": template.id})
        self.assertIn('"comment": "",  # TODO', report.pdfs_build_data_expr)
        report.write({"model": "res.partner"})
        self.assertIn('"comment": html2plaintext(record.comment or ""),', report.pdfs_build_data_expr)
        report.pdfs_build_data_expr = '{"name": record.display_name}'
        report.write({"model": "res.users"})
        self.assertEqual(report.pdfs_build_data_expr, '{"name": record.display_name}')
        # The form's onchange does the same for a report being edited.
        form = Report.new({"name": "Form", "model": "res.bank", "pdfs_build_template_id": template.id})
        form._onchange_pdfs_build_template_id()
        self.assertIn('"comment": "",  # TODO', form.pdfs_build_data_expr)
        form.model_id = self.env["ir.model"]._get("res.partner")
        form._onchange_model_id()
        self.assertIn('"comment": html2plaintext(record.comment or ""),', form.pdfs_build_data_expr)

    def test_lines_skip_sections_and_prices_are_money(self):
        """Mapping rules that only order models exercise, checked on a stand-in model."""
        from odoo.addons.pdfs_build.models.pdfs_build_template import _skeleton_entries

        def field(kind, comodel=None):
            return Mock(type=kind, comodel_name=comodel)

        line = Mock(_fields={
            "name": field("char"), "price_unit": field("float"), "price_subtotal": field("monetary"),
            "display_type": field("selection"), "currency_id": field("many2one", "res.currency"),
        })
        order = Mock(_fields={
            "order_line": field("one2many", "sale.order.line"), "weight": field("float"),
            "currency_id": field("many2one", "res.currency"), "tax_totals": field("binary"),
        })
        order.env = line.env = {"sale.order.line": line}
        properties = {
            "weight": {"type": "string"},
            "tax_totals": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
            "order_line": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "price_unit": {"type": "string"}, "price_subtotal": {"type": "string"},
                }},
            },
        }
        skeleton = "\n".join(_skeleton_entries(properties, {}, 1, order))
        self.assertIn('"weight": format_amount(record.weight, record.currency_id),', skeleton)
        self.assertIn('"tax_totals": tax_groups(record),', skeleton)
        self.assertIn('"price_unit": format_amount(line.price_unit, line.currency_id),', skeleton)
        self.assertIn('"price_subtotal": format_amount(line.price_subtotal, line.currency_id),', skeleton)
        self.assertIn('for line in record.order_line\n        if line.display_type not in ("line_section", "line_note")', skeleton)
        compile("{%s}" % skeleton, "<skeleton>", "eval")

    def test_gallery_wizard_adds_a_design_and_its_report(self):
        """Designs for Odoo: list with previews, add one, get a report on its model."""
        from odoo.addons.pdfs_build.models import pdfs_build_gallery

        listing = {
            "templates": [
                {"id": "odoo-delivery-slip-dock-ticket", "name": "Odoo Delivery Slip — Dock Ticket",
                 "useCase": "odoo-delivery-slip", "category": "odoo", "tier": "expressive", "description": ""},
                {"id": "odoo-invoice-stub-band", "name": "Odoo Invoice — Stub Band", "useCase": "odoo-invoice",
                 "category": "odoo", "tier": "expressive", "description": "Band header."},
            ],
            "categories": [{"id": "odoo", "count": 2}],
        }
        png = Mock(status_code=200, ok=True, headers={"Content-Type": "image/png"}, content=b"\x89PNG")
        names = {entry["id"]: entry["name"] for entry in listing["templates"]}
        calls = []
        added = {}  # what the organization holds after each from-gallery call

        def fake_request(method, url, **kwargs):
            path = url.split("/organizations/org_test/")[1]
            calls.append((method, path, kwargs.get("data")))
            if path == "gallery?category=odoo":
                return response(200, listing)
            if path.endswith("/preview"):
                return png
            if path == "templates/from-gallery":
                gallery_id = json.loads(kwargs["data"])["galleryId"]
                added[gallery_id] = {**LISTED, "externalId": gallery_id, "name": names[gallery_id],
                                     "internalId": "aaaaaaaa-%s" % len(added)}
                return response(201, {"id": added[gallery_id]["internalId"], "externalId": gallery_id,
                                      "galleryId": gallery_id, "name": names[gallery_id],
                                      "status": "published", "publishedVersion": 1})
            if path == "templates":
                return response(200, [LISTED, *added.values()])
            if path.startswith("templates/"):
                external_id = path[len("templates/"):].split("?")[0]
                if external_id in added:
                    return response(200, {**DETAIL, **added[external_id], "schema": self.ODOO_SCHEMA, "sampleData": {}})
                return response(200, DETAIL)
            raise AssertionError("unexpected call %s %s" % (method, url))

        # The test database has no account app; map the invoice to a model it does have.
        with patch.dict(pdfs_build_gallery.USE_CASE_MODELS, {"odoo-invoice": "res.partner"}):
            with patch(REQUEST, side_effect=fake_request):
                action = self.env["pdfs_build.gallery"].action_open()
                wizard = self.env["pdfs_build.gallery"].browse(action["res_id"])
                self.assertEqual(wizard.line_ids.mapped("gallery_id"), ["odoo-invoice-stub-band", "odoo-delivery-slip-dock-ticket"])
                invoice, slip = wizard.line_ids
                self.assertEqual(invoice.document, "Invoice")
                self.assertEqual(invoice.design, "Stub Band")
                self.assertEqual(invoice.model, "res.partner")
                self.assertTrue(invoice.model_installed)
                self.assertEqual(invoice.thumbnail, base64.b64encode(b"\x89PNG"))
                self.assertFalse(invoice.template_id)
                self.assertEqual(slip.model, "stock.picking")
                self.assertFalse(slip.model_installed)
                # Previews are fetched once per design.
                self.assertEqual(len([c for c in calls if c[1].endswith("/preview")]), 2)

                opened = invoice.action_add()
        posted = [c for c in calls if c[1] == "templates/from-gallery"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(json.loads(posted[0][2]), {"galleryId": "odoo-invoice-stub-band",
                                                     "externalId": "odoo-invoice-stub-band",
                                                     "name": "Odoo Invoice — Stub Band"})
        template = self.Template.search([("external_id", "=", "odoo-invoice-stub-band")])
        self.assertTrue(template)
        self.assertEqual(invoice.template_id, template)
        report = self.env["ir.actions.report"].browse(opened["res_id"])
        self.assertEqual(report.model, "res.partner")
        self.assertEqual(report.pdfs_build_template_id, template)
        self.assertNotIn("TODO", report.pdfs_build_data_expr)
        self.assertIn('"name": record.name,', report.pdfs_build_data_expr)
        self.assertTrue(report.binding_model_id)

        # Adding it again neither re-posts nor duplicates the report.
        with patch.dict(pdfs_build_gallery.USE_CASE_MODELS, {"odoo-invoice": "res.partner"}):
            with patch(REQUEST, side_effect=fake_request):
                again = invoice.action_add()
        self.assertEqual(again["res_id"], report.id)
        self.assertEqual(len([c for c in calls if c[1] == "templates/from-gallery"]), 1)

        # A design whose app is not installed is added and synced, with a notice.
        with patch(REQUEST, side_effect=fake_request):
            notice = slip.action_add()
        self.assertEqual(notice["tag"], "display_notification")
        self.assertIn("stock.picking", notice["params"]["message"])

    def test_tax_groups_reads_both_shapes(self):
        currency = self.env.ref("base.USD")
        money = lambda amount: format_amount(self.env, amount, currency)
        record = Mock(env=self.env, currency_id=currency)
        record._fields = {"tax_totals": True, "currency_id": True}
        record.tax_totals = {
            "groups_by_subtotal": {
                "Untaxed Amount": [{"tax_group_name": "VAT 5%", "tax_group_base_amount": 100.0, "tax_group_amount": 5.0}]
            }
        }
        self.assertEqual(tax_groups(record), [{"name": "VAT 5%", "base": money(100.0), "amount": money(5.0)}])
        record.tax_totals = {
            "subtotals": [{
                "name": "Untaxed Amount",
                "tax_groups": [
                    {"group_name": "VAT 5%", "group_label": None, "base_amount_currency": 100.0, "tax_amount_currency": 5.0},
                    {"group_name": "Excise", "group_label": "Excise duty", "base_amount_currency": 40.0, "tax_amount_currency": 8.0},
                ],
            }]
        }
        self.assertEqual(
            tax_groups(record),
            [
                {"name": "VAT 5%", "base": money(100.0), "amount": money(5.0)},
                {"name": "Excise duty", "base": money(40.0), "amount": money(8.0)},
            ],
        )
        record.tax_totals = False
        self.assertEqual(tax_groups(record), [])
        record._fields = {}
        self.assertEqual(tax_groups(record), [])

    def test_skeleton_covers_the_schema(self):
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        skeleton = template._data_expr_skeleton()
        for key in SCHEMA["properties"]:
            self.assertIn('"%s":' % key, skeleton)
        self.assertIn("# Client company name", skeleton)
        wrapped = json.loads(template.schema_json)
        wrapped["properties"]["clientName"]["description"] = "Customer name\nAs shown on the invoice"
        template.schema_json = json.dumps(wrapped)
        multi = template._data_expr_skeleton()
        self.assertIn("# Customer name As shown on the invoice", multi)
        compile(multi, "<skeleton>", "eval")
        self.assertIn('"amount": 0,', skeleton)
        self.assertIn("image(record.image_1920)", skeleton)
        compile(skeleton, "<skeleton>", "eval")
        self.assertIn('"brand": "templates/x/images/brand.png",  # image stored with the template', skeleton)
        self.assertIn("for line in record.line_ids", skeleton)
        wide = {**SCHEMA, "properties": {"field_%02d" % i: {"type": "string"} for i in range(30)}}
        template.schema_json = json.dumps(wide)
        self.assertEqual(len(template.data_fields), 60)
        self.assertTrue(template.data_fields.endswith("..."))
        self.assertEqual(template._report_name(), "pdfs_build.odoo_quotation")

    def test_schema_table_and_draft_suffix(self):
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        html = template.schema_html
        self.assertIn("<code>clientName</code>", html)
        self.assertIn("Client company name", html)
        self.assertIn("<code>items[].amount</code>", html)
        self.assertIn("list of object", html)
        self.assertIn("image (stored with the template)", html)
        self.assertEqual(html.count("&#10003;"), 2, "clientName and items are required")
        self.assertEqual(template.display_name, "Quotation")
        template.status = "draft"
        self.assertEqual(template.display_name, "Quotation (draft)")

    def test_form_helpers_for_the_chosen_model(self):
        from odoo.addons.pdfs_build.models.ir_actions_report import _lines_field
        from odoo.addons.pdfs_build.models.pdfs_build_template import LINES_PLACEHOLDER

        self.assertEqual(_lines_field(["message_ids", "line_ids", "order_line"]), "order_line")
        self.assertEqual(_lines_field(["child_ids", "foo_line_ids"]), "foo_line_ids")
        self.assertIsNone(_lines_field(["child_ids", "bank_ids"]))

        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        report = self.Report.new({"pdfs_build_template_id": template.id})
        report._onchange_pdfs_build_template_id()
        self.assertEqual(report.name, "Quotation")
        self.assertEqual(report.report_type, "qweb-pdf")
        self.assertEqual(report.print_report_name, "object.display_name + ' - ' + 'Quotation'")
        from odoo.tools.safe_eval import safe_eval

        awkward = self.Report.new({"pdfs_build_template_id": template.id})
        template.name = "Invoice (19% VAT) \\ 'draft'"
        awkward._onchange_pdfs_build_template_id()
        self.assertEqual(
            safe_eval(awkward.print_report_name, {"object": self.partner}),
            "Acme Corp - Invoice (19% VAT) \\ 'draft'",
        )
        self.assertIn(LINES_PLACEHOLDER, report.pdfs_build_data_expr)
        report.model_id = self.env["ir.model"]._get("res.partner")
        report._onchange_model_id()
        self.assertEqual(report.model, "res.partner")
        self.assertIn(LINES_PLACEHOLDER, report.pdfs_build_data_expr, "partners have no lines field")
        names = report.pdfs_build_model_field_ids.mapped("name")
        self.assertIn("email", names)
        self.assertIn("child_ids", names)
        self.assertNotIn("message_ids", names)
        self.assertNotIn("activity_ids", names)

        report.pdfs_build_data_expr = "[x for x in %s]" % LINES_PLACEHOLDER
        report.model_id = self.env["ir.model"]._get("res.currency")
        report._onchange_model_id()
        self.assertIn(LINES_PLACEHOLDER, report.pdfs_build_data_expr, "rate_ids is not a lines field")
        report.model_id = self.env["ir.model"]._get("ir.actions.server")
        report._onchange_model_id()
        self.assertIn(LINES_PLACEHOLDER, report.pdfs_build_data_expr)

    def test_editor_link_uses_the_template_page(self):
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        self.assertEqual(
            template.action_open_editor()["url"],
            "https://app.pdfs.build/templates/11111111-2222-3333-4444-555555555555",
        )

    def test_report_goes_to_the_print_menu(self):
        report = self._report('{"clientName": record.name}')
        self.assertEqual(report.binding_model_id.model, "res.partner")
        self.assertEqual(report.binding_type, "report")
        self.assertEqual(report.model_id.model, "res.partner")
        self.assertEqual(report.pdfs_build_template_id._report_name(), "pdfs_build.odoo_quotation_2")
        with self.assertRaises(ValidationError):
            self.Report.create(
                {
                    "name": "Duplicate",
                    "model": "res.partner",
                    "report_name": "pdfs_build.odoo_quotation",
                    "pdfs_build_template_id": report.pdfs_build_template_id.id,
                }
            )

    def test_data_is_fitted_to_the_schema(self):
        report = self._report(
            """{
    "clientName": record.name,
    "clientEmail": record.website,
    "total": record.function,
    "paid": False,
    "items": [{"name": c.name, "amount": None} for c in record.child_ids],
    "since": datetime.date(2026, 1, 2),
    "extra": record.email,
}"""
        )
        data = report._pdfs_build_data(self.partner)
        self.assertEqual(data["clientName"], "Acme Corp")
        self.assertEqual(data["clientEmail"], "", "empty Char is False in Odoo, '' for the schema")
        self.assertEqual(data["total"], 0)
        self.assertIs(data["paid"], False, "booleans stay booleans")
        self.assertEqual(data["items"], [])
        self.assertEqual(data["since"], "2026-01-02")
        self.assertEqual(data["extra"], "john@acme.test")

        self.partner.child_ids = [(0, 0, {"name": "Branch", "type": "other"})]
        data = report._pdfs_build_data(self.partner)
        self.assertEqual(data["items"], [{"name": "Branch", "amount": 0}])

    def test_bad_expressions_are_explained(self):
        report = self._report("record.no_such_field")
        with self.assertRaisesRegex(UserError, "no_such_field"):
            report._pdfs_build_data(self.partner)
        report.pdfs_build_data_expr = '{"clientName": record.child_ids}'
        with self.assertRaisesRegex(UserError, "returns the record"):
            report._pdfs_build_data(self.partner)
        report.pdfs_build_data_expr = "[1, 2]"
        with self.assertRaisesRegex(UserError, "must return a dict"):
            report._pdfs_build_data(self.partner)
        report.pdfs_build_data_expr = ""
        with self.assertRaisesRegex(UserError, "no data expression"):
            report._pdfs_build_data(self.partner)

    def test_image_helper_makes_a_data_url(self):
        from odoo.addons.pdfs_build.models.ir_actions_report import image

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(buffer, "PNG")
        png = buffer.getvalue()
        self.partner.image_1920 = base64.b64encode(png)
        self.assertTrue(image(self.partner.image_1920).startswith("data:image/png;base64,"))
        self.assertTrue(image(png).startswith("data:image/png;base64,"))
        self.assertEqual(image(False), "")

    def test_print_renders_through_pdfs_build(self):
        with patch(REQUEST, return_value=response(200, DETAIL)):
            report = self._report('{"clientName": record.name, "items": []}', pdfs_build_version="staging")
        pdf = blank_pdf()
        with patch(REQUEST, side_effect=api(pdfs=[pdf], version=7)) as request:
            streams = self.Report._render_qweb_pdf_prepare_streams(
                "pdfs_build.odoo_quotation", {}, res_ids=self.partner.ids
            )
        self.assertEqual(streams[self.partner.id]["stream"].getvalue(), pdf)
        self.assertEqual(request.call_args_list[0].args[1], BASE + "/templates/odoo-quotation?version=staging")
        method, url = request.call_args.args
        self.assertEqual((method, url), ("POST", BASE + "/templates/odoo-quotation/render"))
        self.assertEqual(
            posted(request),
            [{"data": {"clientName": "Acme Corp", "items": []}, "version": 7}],
            "the channel is resolved at print time and that exact version is rendered",
        )
        self.assertEqual(request.call_args.kwargs["headers"]["Content-Type"], "application/json")

        other = self.env["res.partner"].create({"name": "Globex"})
        with patch(REQUEST, side_effect=api(pdfs=[pdf, pdf], version=7)) as request:
            content, kind = self.Report.with_context(force_report_rendering=True)._render_qweb_pdf(
                "pdfs_build.odoo_quotation", res_ids=[self.partner.id, other.id]
            )
        self.assertEqual(kind, "pdf")
        self.assertEqual(len(posted(request)), 2, "one render per record")
        self.assertEqual(PdfFileReader(io.BytesIO(content)).getNumPages(), 2, "Odoo merged both PDFs")

    def test_version_selectors(self):
        report = self._report('{"clientName": record.name, "total": record.function}')
        string_total = {**SCHEMA, "properties": {**SCHEMA["properties"], "total": {"type": "string"}}}

        with patch(REQUEST, side_effect=api(pdfs=[blank_pdf()], version=5)) as request:
            report._pdfs_build_render(self.partner)
        self.assertEqual(request.call_args_list[0].args[1], BASE + "/templates/odoo-quotation?version=latest")
        self.assertEqual(posted(request)[0]["version"], 5, "latest is resolved to the promoted version")

        with patch(REQUEST, side_effect=api(pdfs=[blank_pdf()])) as request:
            report._pdfs_build_render(self.partner)
        self.assertEqual(posted(request)[0]["version"], "latest", "an older server: the selector is sent as is")

        with patch(REQUEST, side_effect=api(detail={**DETAIL, "schema": string_total}, version=4)):
            report.pdfs_build_version = "4"
        with patch(REQUEST, side_effect=api(pdfs=[blank_pdf()])) as request:
            report._pdfs_build_render(self.partner)
        self.assertEqual([c.args[0] for c in request.call_args_list], ["POST"], "a number is frozen: no lookup")
        self.assertEqual(posted(request)[0], {"data": {"clientName": "Acme Corp", "total": ""}, "version": 4})

        with patch(REQUEST, side_effect=api(detail=DETAIL, version=8)):
            report.pdfs_build_version = "staging"
        with patch(REQUEST, side_effect=api(pdfs=[blank_pdf()], detail={**DETAIL, "schema": string_total}, version=9)) as request:
            report._pdfs_build_render(self.partner)
        self.assertEqual(
            posted(request)[0],
            {"data": {"clientName": "Acme Corp", "total": ""}, "version": 9},
            "a promoted channel: today's version and its schema, whatever was cached",
        )

    def test_preview_data_shows_what_printing_sends(self):
        """The cached schema of a channel pin is from when the pin was set; after a
        promotion the channel resolves to another version. Preview and print must
        both fit the data to the version that renders."""
        report = self._report('{"clientName": record.name, "total": record.function}')
        string_total = {**SCHEMA, "properties": {**SCHEMA["properties"], "total": {"type": "string"}}}
        with patch(REQUEST, side_effect=api(detail={**DETAIL, "schema": string_total}, version=8)):
            report.pdfs_build_version = "staging"
        self.assertIn('"total": ""', json.dumps(report._pdfs_build_data(self.partner)), "cached: string")

        # staging now points at version 9, where total is a number again
        with patch(REQUEST, side_effect=api(pdfs=[blank_pdf()], detail=DETAIL, version=9)) as request:
            report.action_pdfs_build_preview()
            report._pdfs_build_render(self.partner)
        self.assertIn('"total": 0', report.pdfs_build_preview)
        self.assertIn("template version 9", report.pdfs_build_preview)
        self.assertEqual(posted(request)[0]["data"]["total"], 0, "printing agrees with the preview")

    def test_multi_page_documents_keep_their_records(self):
        report = self._report('{"clientName": record.name}', attachment="'quote-%s.pdf' % object.name")
        other = self.env["res.partner"].create({"name": "Globex"})
        two_pages, one_page = blank_pdf(2), blank_pdf(1)
        with patch(REQUEST, side_effect=api(pdfs=[two_pages, one_page])):
            streams = self.Report._render_qweb_pdf_prepare_streams(
                "pdfs_build.odoo_quotation", {}, res_ids=[self.partner.id, other.id]
            )
        self.assertEqual(list(streams), [self.partner.id, other.id])
        self.assertEqual(PdfFileReader(streams[self.partner.id]["stream"]).getNumPages(), 2)
        self.assertEqual(PdfFileReader(streams[other.id]["stream"]).getNumPages(), 1)

        with patch(REQUEST, side_effect=api(pdfs=[two_pages, one_page])):
            content, _kind = self.Report.with_context(force_report_rendering=True)._render_qweb_pdf(
                "pdfs_build.odoo_quotation", res_ids=[self.partner.id, other.id]
            )
        self.assertEqual(PdfFileReader(io.BytesIO(content)).getNumPages(), 3)
        attachments = self.env["ir.attachment"].search([("res_model", "=", "res.partner"), ("name", "like", "quote-%")])
        self.assertEqual(sorted(attachments.mapped("res_id")), sorted([self.partner.id, other.id]), "one attachment per record")

    def test_duplicate_ids_print_every_occurrence_but_render_once(self):
        report = self._report('{"clientName": record.name, "items": []}')
        pdf = blank_pdf()
        with patch(REQUEST, side_effect=api(pdfs=[pdf])) as request:
            streams = self.Report._render_qweb_pdf_prepare_streams(
                "pdfs_build.odoo_quotation", {}, res_ids=[self.partner.id, self.partner.id]
            )
        self.assertEqual(len(posted(request)), 1, "the record is rendered once")
        self.assertEqual(list(streams), [False], "one combined stream, as Odoo's renderer returns")
        self.assertEqual(PdfFileReader(streams[False]["stream"]).getNumPages(), 2, "both occurrences kept")

        with patch(REQUEST, side_effect=api(pdfs=[pdf])):
            content, _kind = self.Report.with_context(force_report_rendering=True)._render_qweb_pdf(
                "pdfs_build.odoo_quotation", res_ids=[self.partner.id, self.partner.id]
            )
        self.assertEqual(PdfFileReader(io.BytesIO(content)).getNumPages(), 2)

        with self.assertRaisesRegex(UserError, "Select at least one record"):
            self.Report._render_qweb_pdf_prepare_streams("pdfs_build.odoo_quotation", {}, res_ids=[])

    def test_pinned_version_schema_drives_the_fitting(self):
        """What renders is the pinned version, whose data contract may differ
        from the working copy the template shows; the report keeps that
        version's schema and fits the data to it."""
        version_schema = {**SCHEMA, "properties": {**SCHEMA["properties"], "total": {"type": "string"}}}

        def fake(method, url, **kwargs):
            if url.endswith("/templates/odoo-quotation?version=4"):
                return response(200, {**DETAIL, "schema": version_schema, "version": 4})
            return response(404, {"error": "Template not found"})

        report = self._report('{"clientName": record.name, "total": record.function}')
        self.assertEqual(report._pdfs_build_data(self.partner)["total"], 0, "number in the working copy")
        with patch(REQUEST, side_effect=fake) as request:
            report.pdfs_build_version = "4"
        self.assertEqual(request.call_args.args[1], BASE + "/templates/odoo-quotation?version=4")
        self.assertEqual(report._pdfs_build_data(self.partner)["total"], "", "string in version 4")
        self.assertIn("<code>total</code></td><td>string</td>", report.pdfs_build_schema_html)
        with patch(REQUEST, side_effect=fake):
            report.pdfs_build_version = False
        self.assertFalse(report.pdfs_build_schema_json)
        self.assertEqual(report._pdfs_build_data(self.partner)["total"], 0, "back to the template's schema")

    def test_api_errors_reach_the_user(self):
        report = self._report('{"clientName": record.name}')
        validation = {
            "error": "Validation failed",
            "details": [{"path": "", "message": '"items" is a required property'}],
        }
        with patch(REQUEST, side_effect=api(render=response(400, validation))):
            with self.assertRaisesRegex(UserError, '"items" is a required property'):
                report._pdfs_build_render(self.partner)
        plan = {"error": "api_renders_not_allowed_on_free", "message": "REST API renders are not available on the Free plan."}
        with patch(REQUEST, side_effect=api(render=response(402, plan))):
            with self.assertRaisesRegex(UserError, "Starter plan"):
                report._pdfs_build_render(self.partner)
        compilation = {"error": "Compilation failed", "diagnostics": [{"line": 12, "message": "unknown variable"}]}
        with patch(REQUEST, side_effect=api(render=response(422, compilation))):
            with self.assertRaisesRegex(UserError, "line 12: unknown variable"):
                report._pdfs_build_render(self.partner)

    def test_pipeline_runs_end_to_end_whatever_the_load_order(self):
        """Other modules hook the same chain (quotation documents, e-invoice XML)
        and post-process what super() returns; base ends it with wkhtmltopdf. The
        chain must reach base and base must get our PDF, so every module between
        them sees it wherever this module sits in the chain."""
        from odoo.addons.base.models.ir_actions_report import IrActionsReport as Base

        report = self._report('{"clientName": record.name, "items": []}')
        pdf = blank_pdf()
        base_prepare = Base._render_qweb_pdf_prepare_streams
        base_wkhtmltopdf = Base._run_wkhtmltopdf
        with (
            patch(REQUEST, side_effect=api(pdfs=[pdf])),
            patch.object(Base, "_render_qweb_pdf_prepare_streams", autospec=True, side_effect=base_prepare) as base_called,
            patch.object(Base, "_run_wkhtmltopdf", autospec=True, side_effect=base_wkhtmltopdf) as wkhtmltopdf,
            patch.object(Base, "get_wkhtmltopdf_state", return_value="install"),
        ):
            streams = self.Report._render_qweb_pdf_prepare_streams(
                "pdfs_build.odoo_quotation", {}, res_ids=self.partner.ids
            )
        base_called.assert_called_once()
        wkhtmltopdf.assert_not_called()
        self.assertEqual(streams[self.partner.id]["stream"].getvalue(), pdf)

        html, kind = self.Report._render_qweb_html("pdfs_build.odoo_quotation", self.partner.ids)
        self.assertEqual(kind, "html")
        self.assertIn('data-oe-model="res.partner" data-oe-id="%d"' % self.partner.id, html)
        self.assertIn("<main>", html)

    def test_reports_without_template_use_qweb(self):
        with patch(REQUEST) as request:
            with patch(
                "odoo.addons.base.models.ir_actions_report.IrActionsReport._render_qweb_pdf_prepare_streams",
                return_value={},
            ) as core:
                self.Report._render_qweb_pdf_prepare_streams("base.report_irmodulereference", {}, res_ids=[1])
        core.assert_called_once()
        request.assert_not_called()

    def test_previews_use_the_latest_record_and_open_a_dialog(self):
        report = self._report('{"clientName": record.name}')
        latest = self.env["res.partner"].create({"name": "Latest Co"})
        with patch(REQUEST, side_effect=api(version=3)):
            report.action_pdfs_build_preview()
        self.assertIn("Latest Co", report.pdfs_build_preview)
        self.assertIn("template version 3", report.pdfs_build_preview)

        report.pdfs_build_preview_record_id = self.partner.id
        with patch(REQUEST, side_effect=api()):
            report.action_pdfs_build_preview()
        self.assertIn("Acme Corp", report.pdfs_build_preview, "the chosen record wins over the latest one")
        report.pdfs_build_preview_record_id = 999999999
        with patch(REQUEST, side_effect=api()):
            report.action_pdfs_build_preview()
        self.assertIn("Latest Co", report.pdfs_build_preview, "a stale choice falls back to the latest record")
        report.pdfs_build_preview_record_id = self.partner.id
        report.model_id = self.env["ir.model"]._get("res.currency")
        report._onchange_model_id()
        self.assertFalse(report.pdfs_build_preview_record_id, "cleared when the model changes")
        report.model_id = self.env["ir.model"]._get("res.partner")
        report._onchange_model_id()

        pdf = blank_pdf()
        with patch(REQUEST, side_effect=api(pdfs=[pdf])) as request:
            action = report.action_pdfs_build_preview_pdf()
        self.assertEqual(posted(request)[0]["data"], {"clientName": "Latest Co"})
        self.assertEqual(base64.b64decode(report.pdfs_build_preview_pdf), pdf)
        self.assertEqual((action["target"], action["res_id"]), ("new", report.id))
        self.assertEqual(action["views"], [(self.env.ref("pdfs_build.view_report_preview_form").id, "form")])
        self.assertIn("Latest Co", action["name"])

        template = report.pdfs_build_template_id
        with patch(REQUEST, side_effect=api(pdfs=[pdf])) as request:
            action = template.action_preview_sample()
        self.assertEqual(posted(request), [{"data": SAMPLE}])
        self.assertEqual(base64.b64decode(template.preview_pdf), pdf)
        self.assertEqual((action["target"], action["res_id"], action["res_model"]), ("new", template.id, "pdfs_build.template"))

    def test_existing_report_can_be_taken_over(self):
        self._sync()
        template = self.Template.search([("external_id", "=", "odoo-quotation")])
        core = self.Report.create(
            {
                "name": "Contacts",
                "model": "res.partner",
                "report_type": "qweb-pdf",
                "report_name": "test.contact_card",
                "print_report_name": "'Card - %s' % object.name",
                "binding_model_id": self.env["ir.model"]._get("res.partner").id,
            }
        )
        picker = template.action_use_for_existing_report()
        self.assertEqual(picker["res_model"], "ir.actions.report")
        self.assertIn(core, self.Report.search(picker["domain"]))

        action = core.with_context(**picker["context"]).action_pdfs_build_use_template()
        self.assertEqual(core.pdfs_build_template_id, template)
        self.assertEqual(core.name, "Contacts", "the report keeps its identity")
        self.assertEqual(core.report_name, "test.contact_card")
        self.assertEqual(core.print_report_name, "'Card - %s' % object.name")
        self.assertIn('"clientName":', core.pdfs_build_data_expr)
        self.assertNotIn(core, self.Report.search(picker["domain"]), "no longer offered for take-over")
        self.assertEqual(action["res_id"], core.id)
        with self.assertRaisesRegex(UserError, "Use for Existing Report"):
            core.action_pdfs_build_use_template()

    def test_settings_buttons_save_and_use_the_form_values(self):
        settings = self.env["res.config.settings"].create(
            {"pdfs_build_api_key": " prs_new ", "pdfs_build_organization_id": "org_new"}
        )
        with patch(REQUEST, return_value=response(200, [LISTED])) as request:
            action = settings.action_pdfs_build_test_connection()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(request.call_args.args[1], "https://api.pdfs.build/v2/organizations/org_new/templates")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer prs_new")
        self.assertEqual(self.Template._api_settings()["api_key"], "prs_new")
        self.assertEqual(self.Template._api_settings()["url"], "https://api.pdfs.build")
