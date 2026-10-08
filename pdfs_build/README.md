# pdfs.build PDF Templates for Odoo

Print any Odoo record with a template designed on [pdfs.build](https://pdfs.build)
instead of a QWeb layout. Quotations, invoices, delivery slips, certificates,
statements: design the document once in the pdfs.build editor, then map Odoo
fields to it with a Python expression. The report lands in the record's
**Print** menu and works in email templates and everywhere else Odoo prints.

Odoo 17 (Community and Enterprise). License LGPL-3.

## Requirements

- A pdfs.build account on the **Starter plan or higher**. The Free plan has no
  REST API access, so it cannot create API keys.
- At least one **published** template. Drafts cannot be rendered over the API.

## Setup

1. Install the module (Apps > search "pdfs.build").
2. In the pdfs.build app, open **Developers > API keys**, create a key (it starts
   with `prs_` and is shown once) and copy the **Organization ID** shown at the
   top of the same page.
3. In Odoo, open **pdfs.build > Settings**, paste both values, click
   **Test Connection**, then **Sync Templates**.
4. **Preview Sample PDF** on a template renders its sample data and shows the
   PDF in Odoo. Archive the templates you do not print from Odoo (select them in the list,
   Actions > Archive). The sync keeps archived templates archived, and they
   stay out of the report pickers.

## Creating a report

1. **pdfs.build > Templates**, open a template, click **New Report**.
2. Pick the Odoo **Model** (for example Sales Order).
3. The **Data** tab is pre-filled with one entry per field of the template's
   schema, and the line-item loop already points at the model's lines field
   when there is an obvious one. The **Template Fields** tab lists what the
   template expects, **Odoo Fields** lists the model's fields with their
   labels, and **Help** has a worked example. Replace the placeholders with
   the record's fields:

   ```python
   {
       "quoteNumber": record.name,
       "clientName": record.partner_id.commercial_partner_id.name,
       "clientEmail": record.partner_id.email,
       "issueDate": format_date(record.date_order),
       "items": [
           {"name": line.product_id.name, "description": line.name, "amount": line.price_subtotal}
           for line in record.order_line
       ],
       "total": record.amount_total,
       "logo": image(record.company_id.logo),
   }
   ```

4. **Preview Data** shows the JSON a record would send and **Preview PDF**
   renders it through pdfs.build and shows the PDF in a dialog (download and
   print from its toolbar). Both use the **Preview Record** you pick, or the
   latest record of the model. Save: the report is in the model's **Print** menu.

## Replacing Odoo's own invoice or quotation PDF

To make pdfs.build render a report Odoo already has, instead of adding a new
Print menu entry, open the template and click **Use for Existing Report**,
select the report (for example *Invoices* or *Quotation / Order*) and click
**Use Template**. The report keeps its name, technical name, file name and
attachment settings; only the rendering changes. From then on every place
Odoo prints that report uses the template: the Print menu, "Send by email"
attachments, the customer portal download, and the PDF Odoo stores on a
posted invoice. Odoo's own post-processing of these reports (embedding the
Factur-X or UBL XML in invoices, the quotation document builder) still runs on
the pdfs.build PDF. To go back, clear the pdfs.build template on the report.

Available in the expression: `record` (also `object`), `env`, `user`, `company`,
`datetime`, `dateutil`, `relativedelta`, `time`, `image(binary)` (an image or
binary field as the data URL image fields expect), `html2plaintext(html)`,
`format_date(value)`, `format_datetime(value)`, `format_amount(amount, currency)`.
Empty Odoo fields (`False`) are sent as `""`, `0`, `[]` or `{}` according to the
schema; dates become ISO strings. Returning a recordset is refused with a hint.

Printing several records renders one PDF per record and merges them, as Odoo
does for its own reports (a record selected twice is rendered once and
appears twice).

The synced schema and sample data are those of the template's latest
promoted version, the one a render uses by default. **Template Version** pins
what a report renders: a version number is frozen and its schema is kept on
the report; a channel (`latest`, `staging`...) is resolved every time the
report prints, the data is fitted to the schema of the version it points at,
and that exact version is rendered, so promoting a channel never breaks a
report. (On a pdfs.build server that predates the `version` selector on the
template endpoint, the working copy is used instead.)

## Replacing Odoo's own invoice or quotation PDF

To make pdfs.build render a report Odoo already has, instead of adding a new
Print menu entry, open the template and click **Use for Existing Report**,
select the report (for example *Invoices* or *Quotation / Order*) and click
**Use Template**. The report keeps its name, technical name, file name and
attachment settings; only the rendering changes. From then on every place
Odoo prints that report uses the template: the Print menu, "Send by email"
attachments, the customer portal download, and the PDF Odoo stores on a
posted invoice. Odoo's own post-processing of these reports (embedding the
Factur-X or UBL XML in invoices, the quotation document builder) still runs on
the pdfs.build PDF. To go back, clear the pdfs.build template on the report.

Available in the expression: `record` (also `object`), `env`, `user`, `company`,
`datetime`, `dateutil`, `relativedelta`, `time`, `image(binary)` (an image or
binary field as the data URL image fields expect), `html2plaintext(html)`,
`format_date(value)`, `format_datetime(value)`, `format_amount(amount, currency)`.
Empty Odoo fields (`False`) are sent as `""`, `0`, `[]` or `{}` according to the
schema; dates become ISO strings. Returning a recordset is refused with a hint.

Printing several records renders one PDF per record and merges them, as Odoo
does for its own reports (a record selected twice is rendered once and
appears twice).

The synced schema and sample data are the template's current working copy on
pdfs.build. Rendering validates against the version actually rendered: the
latest promoted version, or the one pinned in **Template Version**. If the
working copy has unpromoted edits that change a field's type, the empty-field
fitting described above follows the working copy; promote the template (or
sync after promoting) so both match. **Advanced > Save as Attachment Prefix** and
**Reload from Attachment** behave as on any Odoo report. **Template Version**
pins a version number, `draft`, or a channel such as `staging`.

## Data sent to pdfs.build

Each print sends the data returned by the report's expression, with the API key
in the `Authorization` header, to `https://api.pdfs.build`. Nothing else
leaves Odoo. Render logs on pdfs.build keep the data
payload for your audit trail; see pdfs.build's privacy policy.

## Development

```bash
odoo -d test -i pdfs_build --test-enable --test-tags /pdfs_build --stop-after-init
```

The tests mock the HTTP layer; no account is needed to run them.
