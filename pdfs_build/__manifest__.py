{
    "name": "pdfs.build PDF Templates",
    "summary": "Print any Odoo document with a template designed on pdfs.build",
    "description": """
Print quotations, invoices, delivery slips, certificates or any other Odoo
record with a template designed on pdfs.build, instead of a QWeb layout.

1. Enter your pdfs.build API key and organization ID in Settings.
2. Sync the published templates into Odoo.
3. For each template, create a report: pick the Odoo model and write the
   Python expression that turns a record into the template's data. Or click
   Designs for Odoo: the gallery's Odoo designs are named after Odoo's
   fields, and one click adds the design, syncs it and creates its report.
4. The report appears in the record's Print menu, in email templates and
   everywhere else Odoo prints PDFs. Printing several records merges the PDFs.

This module sends the data produced by your expression, together with your
API key, to the pdfs.build API (api.pdfs.build) every time a report is printed.
Rendering over the API needs a pdfs.build Starter plan or higher.
""",
    "author": "pdfs.build",
    "website": "https://pdfs.build",
    "support": "hello@pdfs.build",
    "category": "Productivity",
    "version": "19.0.1.2.0",
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
