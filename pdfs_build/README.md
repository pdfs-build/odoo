# Invoice & Report Designer by pdfs.build

Print any Odoo record with a template designed on [pdfs.build](https://pdfs.build)
instead of a QWeb layout: invoices, quotations, purchase orders, delivery
slips, certificates, statements. Add a ready-made design in one click, or
design your own in the pdfs.build editor and map Odoo fields to it. The report
lands in the record's **Print** menu and works in email templates and
everywhere else Odoo prints.

Odoo 19 (Community and Enterprise). License LGPL-3.

## Requirements

- A pdfs.build account with REST API access. New accounts start with a
  [7-day free trial](https://app.pdfs.build/signup?plan=pro&interval=monthly)
  of the Pro plan, which includes the API. After the trial, plans start at
  $19 a month.
- At least one **published** template, or a design added from **Designs for
  Odoo**. Drafts cannot be rendered over the API.

## Setup

1. Install the module (Apps > search "pdfs.build").
2. In the pdfs.build app, open **Developers > API keys**, create a key (it
   starts with `prs_` and is shown once) and copy the **Organization ID**
   shown at the top of the same page.
3. In Odoo, open **pdfs.build > Settings**, paste both values, click
   **Test Connection**, then **Sync Templates**.
4. **Preview Sample PDF** on a template renders its sample data and shows the
   PDF in Odoo. Archive the templates you do not print from Odoo (select them
   in the list, then Actions > Archive). The sync keeps archived templates
   archived, and they stay out of the report pickers.

## Designs for Odoo

The **Designs for Odoo** button, in **pdfs.build > Templates** and in the
settings, lists the gallery's invoice,
quotation, purchase order and delivery slip designs, with previews. Their
fields are named after Odoo's own, so they need no mapping: **Add to Odoo**
copies the design into your organization, syncs it and creates its report,
with the data expression already written. The designs take the company logo
and document-layout colors from Odoo's settings.

## Creating a report

1. **pdfs.build > Templates**, open a template, click **New Report**.
2. Pick the Odoo **Model** (for example Sales Order).
3. The **Data** tab is pre-filled with one entry per field of the template's
   schema. Every field whose name matches one of the model's is read
   automatically, nested records and lines included, and the line-item loop
   already points at the model's lines field when there is an obvious one.
   The **Template Fields** tab lists what the template expects, **Odoo
   Fields** lists the model's fields with their labels, and **Help** has a
   worked example. Replace the remaining placeholders with the record's
   fields:

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

4. **Preview Data** shows the JSON a record would send, and **Preview PDF**
   renders it through pdfs.build and shows the PDF in a dialog (download and
   print from its toolbar). Both use the **Preview Record** you pick, or the
   latest record of the model. Save: the report is in the model's **Print**
   menu.

### The data expression

Available in the expression: `record` (also `object`), `env`, `user`,
`company`, `datetime`, `dateutil`, `relativedelta`, `time`, `image(binary)`
(an image or binary field as the data URL image fields expect),
`html2plaintext(html)`, `format_date(value)`, `format_datetime(value)`,
`format_amount(amount, currency)`, `tax_groups(record)` (the taxes of an
invoice or order as a list of `{"name", "base", "amount"}`) and
`weight_uom_name` (the weight unit of the Inventory settings, "kg" or "lb").

Empty Odoo fields (`False`) are sent as `""`, `0`, `[]` or `{}` according to
the schema, and dates become ISO strings. Returning a recordset is refused
with a hint.

## Replacing Odoo's own invoice or quotation PDF

To make pdfs.build render a report Odoo already has, instead of adding a new
Print menu entry, open the template and click **Use for Existing Report**,
select the report (for example *Invoices* or *Quotation / Order*) and click
**Use Template**. The report keeps its name, technical name, file name and
attachment settings; only the rendering changes. From then on, every place
Odoo prints that report uses the template: the Print menu, "Send by email"
attachments, the customer portal download, and the PDF Odoo stores on a
posted invoice. Odoo's own post-processing of these reports (embedding the
Factur-X or UBL XML in invoices, the quotation document builder) still runs on
the pdfs.build PDF. To go back, clear the pdfs.build template on the report.

Such a report keeps Odoo's layout as a fallback. When pdfs.build cannot render
(it is unreachable, has a server error, or the plan or monthly quota is used
up), the report prints with Odoo's own layout instead of failing and the
server log says why, so an outage or an expired plan does not stop invoices
going out. Turn off **Fall Back to Odoo's Layout** on the report to get the
error instead. A validation error in the data is never hidden this way.

## Language

Each record is printed in its partner's language, as Odoo prints its own
documents: an invoice or an order in its customer's language, a contact in
its own. Translated fields, `format_date`, `format_datetime` and
`format_amount` follow it. Records without a partner, or whose partner's
language is not installed, use the language of the user printing.

## Printing several records

Printing several records renders one PDF per record and merges them, as Odoo
does for its own reports (a record selected twice is rendered once and
appears twice). The template version is resolved once for the whole print,
and up to four renders run at the same time. Records whose saved PDF Odoo
reloads are not rendered again.

## Template versions

The synced schema and sample data are those of the template's latest
promoted version, the one a render uses by default. **Template Version** pins
what a report renders. A version number is frozen, and its schema is kept on
the report. A channel (`latest`, `staging`...) is resolved every time the
report prints: the data is fitted to the schema of the version it points at,
and that exact version is rendered, so promoting a channel never breaks a
report. (On a pdfs.build server that predates the `version` selector on the
template endpoint, the working copy is used instead.)

**Advanced > Save as Attachment Prefix** and **Reload from Attachment** behave
as on any Odoo report.

## Data sent to pdfs.build

Each print sends the data returned by the report's expression, with the API
key in the `Authorization` header, to `https://api.pdfs.build`. Nothing else
leaves Odoo. Render logs on pdfs.build keep the data payload for your audit
trail; see pdfs.build's privacy policy.

## Development

```bash
odoo -d test -i pdfs_build --test-enable --test-tags /pdfs_build --stop-after-init
```

The tests mock the HTTP layer; no account is needed to run them.
