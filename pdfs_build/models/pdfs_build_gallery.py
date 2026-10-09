import base64

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# The Odoo model each gallery document type is built for. A design whose model
# is not installed can still be added to the organization; the report waits.
USE_CASE_MODELS = {
    "odoo-invoice": "account.move",
    "odoo-quotation": "sale.order",
    "odoo-purchase-order": "purchase.order",
    "odoo-delivery-slip": "stock.picking",
}
GALLERY_CATEGORY = "odoo"
# The order a business meets the documents in, not the alphabet's.
USE_CASE_SEQUENCE = {"odoo-invoice": 10, "odoo-quotation": 20, "odoo-purchase-order": 30, "odoo-delivery-slip": 40}


class PdfsBuildGallery(models.TransientModel):
    """The designs pdfs.build built for Odoo, one click away from a report.

    Opening the dialog lists the gallery's Odoo category with previews; adding a
    design copies it into the organization (published), syncs, and creates the
    report on the model it was built for -- a quotation design becomes a report
    on sale.order whose data expression the module writes itself.
    """

    _name = "pdfs_build.gallery"
    _description = "pdfs.build Designs for Odoo"

    line_ids = fields.One2many("pdfs_build.gallery.line", "wizard_id", string="Designs")

    @api.model
    def action_open(self):
        wizard = self.create({"line_ids": [(0, 0, values) for values in self._gallery_lines()]})
        return {
            "type": "ir.actions.act_window",
            "name": _("Designs for Odoo"),
            "res_model": self._name,
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
            "context": {"dialog_size": "extra-large"},
        }

    @api.model
    def _gallery_lines(self):
        """One line per design of the gallery's Odoo category, newest sync first."""
        Template = self.env["pdfs_build.template"]
        settings = Template._api_settings()
        listing = Template._api_request("GET", "gallery?category=%s" % GALLERY_CATEGORY, settings=settings)
        added = {
            t.external_id: t.id
            for t in Template.with_context(active_test=False).search([("external_id", "in", [
                entry.get("id") for entry in listing.get("templates", [])
            ])])
        }
        lines = []
        for entry in listing.get("templates", []):
            use_case = entry.get("useCase") or ""
            model = USE_CASE_MODELS.get(use_case, "")
            name = entry.get("name") or entry.get("id")
            lines.append({
                "sequence": USE_CASE_SEQUENCE.get(use_case, 90),
                "gallery_id": entry.get("id"),
                "name": name,
                # "Odoo Invoice — Stub Band" is the template's name; the design is "Stub Band".
                "design": name.split(" — ")[-1] if " — " in name else name,
                "use_case": use_case,
                "document": self._document_label(use_case),
                "description": entry.get("description") or "",
                "tier": entry.get("tier") or "",
                "model": model,
                "model_installed": bool(model) and model in self.env,
                "template_id": added.get(entry.get("id"), False),
                "thumbnail": self._thumbnail(entry.get("id"), settings),
            })
        # Sorted here as well as by _order: a record created in this transaction
        # keeps its lines in creation order until the cache is reloaded.
        return sorted(lines, key=lambda line: (line["sequence"], line["name"]))

    @api.model
    def _document_label(self, use_case):
        return {
            "odoo-invoice": _("Invoice"),
            "odoo-quotation": _("Quotation / Sales Order"),
            "odoo-purchase-order": _("Purchase Order"),
            "odoo-delivery-slip": _("Delivery Slip"),
        }.get(use_case, use_case.replace("odoo-", "").replace("-", " ").title())

    @api.model
    def _thumbnail(self, gallery_id, settings):
        """The design's page-1 render, or nothing: a missing picture is not an error."""
        try:
            image = self.env["pdfs_build.template"]._api_request(
                "GET", "gallery/%s/preview" % gallery_id, settings=settings, timeout=60
            )
        except UserError:
            return False
        return base64.b64encode(image) if isinstance(image, bytes) else False


class PdfsBuildGalleryLine(models.TransientModel):
    _name = "pdfs_build.gallery.line"
    _description = "pdfs.build Design for Odoo"
    _order = "sequence, name"

    wizard_id = fields.Many2one("pdfs_build.gallery", required=True, ondelete="cascade")
    sequence = fields.Integer(default=90)
    gallery_id = fields.Char(string="Design ID", required=True)
    name = fields.Char(required=True)
    design = fields.Char()
    use_case = fields.Char()
    document = fields.Char(string="Document")
    description = fields.Text()
    tier = fields.Char()
    model = fields.Char(string="Odoo Model")
    model_installed = fields.Boolean()
    thumbnail = fields.Binary(attachment=False)
    template_id = fields.Many2one("pdfs_build.template", string="In Odoo as")
    report_ids = fields.One2many("ir.actions.report", compute="_compute_report_ids")

    @api.depends("template_id")
    def _compute_report_ids(self):
        for line in self:
            line.report_ids = line.template_id.report_ids if line.template_id else False

    def action_add(self):
        """Copy the design into the organization, sync it, and create its report."""
        self.ensure_one()
        Template = self.env["pdfs_build.template"]
        template = self.template_id or Template.with_context(active_test=False).search(
            [("external_id", "=", self.gallery_id)], limit=1
        )
        if not template:
            Template._api_request(
                "POST",
                "templates/from-gallery",
                json_body={"galleryId": self.gallery_id, "externalId": self.gallery_id, "name": self.name},
            )
            Template.action_sync()
            template = Template.with_context(active_test=False).search([("external_id", "=", self.gallery_id)], limit=1)
            if not template:
                raise UserError(_("%s was added on pdfs.build but did not come back in the sync.", self.name))
        template.active = True
        self.template_id = template

        if not self.model_installed:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "type": "warning",
                    "title": _("Design added"),
                    "message": _(
                        "%(name)s is in your pdfs.build organization and synced. Install the app that "
                        "provides %(model)s to print it from Odoo.",
                        name=self.name,
                        model=self.model,
                    ),
                    "next": {"type": "ir.actions.act_window_close"},
                },
            }

        report = self.env["ir.actions.report"].search(
            [("pdfs_build_template_id", "=", template.id), ("model", "=", self.model)], limit=1
        )
        if not report:
            report = self.env["ir.actions.report"].create({
                "name": template.name,
                "model": self.model,
                "pdfs_build_template_id": template.id,
            })
        return {
            "type": "ir.actions.act_window",
            "name": report.name,
            "res_model": "ir.actions.report",
            "res_id": report.id,
            "views": [(self.env.ref("pdfs_build.view_report_form").id, "form")],
        }
