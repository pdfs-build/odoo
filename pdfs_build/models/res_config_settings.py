from odoo import _, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    pdfs_build_api_key = fields.Char(
        string="API Key",
        config_parameter="pdfs_build.api_key",
        help="Created under Developers > API keys in the pdfs.build app. It starts with prs_ and is shown once.",
    )
    pdfs_build_organization_id = fields.Char(
        string="Organization ID",
        config_parameter="pdfs_build.organization_id",
        help="Shown at the top of Developers > API keys. It must be the organization the API key belongs to.",
    )

    def _pdfs_build_settings(self):
        """The connection settings as entered in the form, saved so later steps use them too."""
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("pdfs_build.api_key", (self.pdfs_build_api_key or "").strip())
        icp.set_param("pdfs_build.organization_id", (self.pdfs_build_organization_id or "").strip())
        return self.env["pdfs_build.template"]._api_settings()

    def action_pdfs_build_test_connection(self):
        settings = self._pdfs_build_settings()
        templates = self.env["pdfs_build.template"]._api_request("GET", "templates", settings=settings)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Connected to pdfs.build"),
                "message": _("%s published template(s) in this organization.", len(templates)),
            },
        }

    def action_pdfs_build_sync(self):
        self._pdfs_build_settings()
        return self.env["pdfs_build.template"].action_sync()
