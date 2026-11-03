{
    "name": "Invoice & Report Designer by pdfs.build",
    "summary": "Custom invoice, quotation, purchase order and delivery slip layouts, designed visually. No QWeb.",
    "description": """
Print invoices, quotations, purchase orders, delivery slips or any other Odoo
record with a template designed on pdfs.build, instead of a QWeb layout.

1. Enter your pdfs.build API key and organization ID under pdfs.build > Settings.
2. Click Designs for Odoo and add a ready-made design: it is copied into your
   organization and its report is created, with no mapping to write. Or sync
   your own templates and create a report for each: pick the Odoo model, and
   the data expression is written from the template's fields.
3. The report appears in the record's Print menu, in email templates and
   everywhere else Odoo prints PDFs, or replaces one of Odoo's own reports.
   Printing several records merges the PDFs.

Each record prints in its partner's language. A report that replaces one of
Odoo's own falls back to Odoo's layout when pdfs.build cannot render.

This module sends the data produced by the report, together with your API key,
to the pdfs.build API (api.pdfs.build) every time a report is printed. New
pdfs.build accounts start with a 7-day free trial that includes the API; plans
start at $19 a month after it.
""",
    "author": "pdfs.build",
    "website": "https://pdfs.build",
    "support": "hello@pdfs.build",
    "category": "Productivity",
    "version": "18.0.1.3.0",
    "license": "LGPL-3",
    "depends": ["base", "web"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "views/pdfs_build_gallery_views.xml",
        "views/pdfs_build_template_views.xml",
        "views/ir_actions_report_views.xml",
        "views/res_config_settings_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": ["pdfs_build/static/src/scss/preview.scss"],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
}
