# Salesforce → Odoo 19 Field Mapping

Source: SFDC field list (requirements doc), `odoo_fields.json` exported from
`OdooSandbox19` on mpl-odoo (server 19.0-20260928), and `sf sobject describe`
output for Account, Opportunity and OpportunityLineItem from the `mtab--staging`
sandbox (2026-10-12).

Status key:
- **OK**: maps to an existing stock Odoo field
- **LOOKUP**: the SF value is a name/email/ISO code that must be resolved to an Odoo record ID
- **NEW**: no Odoo field exists; added by the `sf_connector` Odoo module (see below)
- **EXISTS (lb)**: field already provided by `lb_item_deliverables`
- **DECIDE**: needs a business decision before it can be mapped

---

## 1. Account → `res.partner`

| SFDC field | Odoo field | Status | Notes |
|---|---|---|---|
| `Id` | `sf_account_id` | NEW | Unique, indexed. The upsert key. |
| `Name` | `name` | OK | `is_company = True` on create. |
| `Legal_Entity_Name__c` | `sf_legal_entity_name` | NEW | Listed twice in the source doc; mapped once. |
| `Phone` | `phone` | OK | |
| `Fax` | — | DECIDE | Exists in SF (phone type). Odoo 19 has no fax field. Drop it, or add `sf_fax`. |
| `Website` | `website` | OK | |
| `Description` | `comment` | OK | Odoo field is HTML; plain text is escaped. |
| `VAT_Number_Text__c` | `vat` | OK | Read-only SF formula over `VAT_Number__c` (textarea). Fine to read. Odoo validates VAT format per country if `base_vat` is installed, so malformed SF values may be rejected. |
| `mTab_Billing_Entity__c` | `sf_default_billing_entity_id` | LOOKUP | Picklist: `mTab LLC`, `G.A.M.M.A`. Needs a map to Odoo `res.company` records. See §4.1. |
| `Account_Manager__r.Email` | `user_id` | LOOKUP | `res.users` by login/email. Unmatched → leave blank and log a warning. |
| `CurrencyIsoCode` | `property_product_pricelist` | LOOKUP | Picklist: GBP, CNY, EUR, JPY, USD. Partners have no writable currency (`currency_id` is computed, read-only). Currency is carried by the pricelist, so one pricelist per currency is needed. See §4.2. |
| `Billing_Distribution_Method__c` | `sf_billing_distribution_method` | NEW | Free text in SF (string 100), not a picklist. Stored as Char. |
| `ParentId` | `parent_id` | LOOKUP | Resolve by the parent's `sf_account_id`, not by name (names aren't unique). The parent must be synced first. |
| `BillingStreet` / `BillingCity` / `BillingState` / `BillingCountry` / `BillingPostalCode` | `street` / `city` / `state_id` / `country_id` / `zip` | OK + LOOKUP | SF state/country picklists are **not** enabled (no `BillingCountryCode`), so country and state are free text. Match by name to `res.country` / `res.country.state` with an alias table for variants ("USA", "UK"…). Unmatched → leave blank and log. |
| `ShippingStreet` … `ShippingPostalCode` | child `res.partner` with `type = 'delivery'` | OK + LOOKUP | Odoo stores alternate addresses as child contacts. Created only when shipping differs from billing. Same free-text country caveat. |

**Write-back to SFDC:** Odoo partner ID and record URL → new SF fields on
Account. Celigo uses the same pattern today (`celigo_sfnsio__NetSuite_Id__c`,
`celigo_sfnsio__NetSuite_Record__c`, `celigo_sfnsio__NetSuite_Sync_Error__c`), so
the admin should create matching `Odoo_Id__c`, `Odoo_Record__c` (URL) and
`Odoo_Sync_Error__c` fields.

---

## 2. Opportunity → `sale.order`

| SFDC field | Odoo field | Status | Notes |
|---|---|---|---|
| `Id` | `sf_opportunity_id` | NEW | Unique, indexed. Upsert key. |
| `Account.Id` | `partner_id` | LOOKUP | Via `sf_account_id`. Locked after create. |
| `Name` | `origin` | OK | `name` stays Odoo's own SO sequence (S000xx). |
| `PO_Number__c` | `client_order_ref` | OK | Stock "Customer Reference"; prints on invoices. |
| `mTab_Billing_Entity__c` | `company_id` | LOOKUP | `mTab LLC` or `G.A.M.M.A`. The placeholder value `This will prepopulate on save` means not set → reject the order. Locked after create. |
| `Revenue_allocation_company_ID__c` | `sf_revenue_allocation_company_id` | NEW — **fix type** | In SF this is a lookup to **Account**, not to an mTab entity. The Odoo field must be many2one `res.partner` (resolved via `sf_account_id`), not `res.company`. |
| `CurrencyIsoCode` | via `pricelist_id` | LOOKUP | GBP, CNY, EUR, JPY, USD. SO `currency_id` is read-only and comes from the pricelist. Locked after create. |
| `Pricebook2Id` | `pricelist_id` | LOOKUP / DECIDE | Needs a Pricebook → pricelist map. Pricelist currency must match `CurrencyIsoCode`. |
| `Payment_Terms__c` | `payment_term_id` | LOOKUP | 12 values: Net 30, Net 30 EOM, Net 45, Net 45 PSA, Net 55, Net 60, Net 60 EOM, Net 65, Net 75, Net 90, Net 120, Net 180. Each needs a matching `account.payment.term` in Odoo (match by name). Placeholder `This will prepopulate on save` → reject. |
| `OwnerId` → `Owner.Email` | `user_id` | LOOKUP | `res.users` by email. |
| `Date_Award_Won_Date__c` | `sf_award_won_date` | NEW | **Changed from `Date_Award_Won0__c`**, which is a text formula over a text field. `Date_Award_Won_Date__c` is a real date field. Locked after create. `date_order` gets set to this too. |
| `Term_Start_Date__c` | `sf_term_start_date` | NEW | |
| `Type` | `sf_opportunity_type` | NEW | Picklist: New, Up-Sell, Cross-Sell, Renewal. Can become a Selection. |
| `Description` | `sf_description` | NEW | Internal; not printed. |
| `Invoice_Notes__c` | `sf_invoice_notes` | NEW — **changed** | SF label is "Invoicing Instructions/Notes (internal)". It's internal, so it must **not** go in `note`, which prints on the quotation and invoice. |
| `Additional_information_to_invoice__c` | `sf_invoice_additional_info` | NEW | SF label is "Additional Info displayed on Invoice" (255 chars). This is the customer-facing one; needs an invoice report tweak to print. |
| `VAT_Tax_Number__c` | — | DECIDE | Odoo takes VAT from the invoice partner. If the order VAT can differ from the account VAT, it must go on the invoice address partner. |
| `Billing_Distribution_Method__c` | `sf_billing_distribution_method` | NEW | Free text, same as Account. |
| `Parent_Account__c` | — | OK (derived) | Lookup to Account. Already implied by `partner_id.parent_id`. Not stored. |
| `Billing_Street__c` / `Billing_City__c` / `Billing_State_Province__c` / `Billing_Zip_Postal_Code__c` | `partner_invoice_id` | LOOKUP | Find-or-create a child partner `type = 'invoice'` under the customer with this address. |
| `Billing Address` | — | — | Compound duplicate of the four fields above. |

**Status on create:** `state = 'draft'` (Quotation). This is the "pending approval"
state; finance clicks *Confirm* to move it to `sale`, which is what releases it to
the deliverables app. Odoo has no separate "pending approval" state; if finance
wants that label visibly, add an approval flag in the connector module.

**Write-back to SFDC:** SO ID, SO number and URL → new SF fields on Opportunity.

---

## 3. OpportunityLineItem → `sale.order.line`

The `sale.order.line` model currently carries **two parallel sets** of custom
fields for the same data:

| Concept | `lb_item_deliverables` field | Manual (Studio-style) field |
|---|---|---|
| Year of study | `year_of_study` | `x_year_of_study` |
| Country | `country_region` | `x_country` |
| Wave | (on deliverable: `wave_label`, `wave_number`) | `x_wave`, `x_wave_no` |
| Delivery date | (on deliverable: `ship_date`) | `x_delivery_date` |
| Delivered by | (on deliverable: `delivered_by_name`) | `x_delivered_by` |
| Dispatchable | (on deliverable: `dispatch_status`) | `x_dispatchable` |

The connector will write to the **module fields only**. The `x_` duplicates look
like leftovers from before the module; worth confirming nothing reads them and then
removing them.

| SFDC field | Odoo field | Status | Notes |
|---|---|---|---|
| `Id` | `sf_line_id` | NEW | Upsert key per line. Also copied to `lb.item.deliverable.sf_opportunity_line_id` (already exists there). |
| `celigo_sfnsio__NetSuite_Line_Id__c` | — | DECIDE | Legacy Celigo/NetSuite key. Replaced by `sf_line_id`. Keep for migration matching only? |
| `Product2Id` (→ `Product2.ProductCode`) | `product_id` | LOOKUP | Match on product code → `default_code`, which is more reliable than `rapidproducts__ProductName__c` (free text). Fall back to name. Unmatched → error, line not created. Requires Odoo products to carry the SF product codes. |
| `Combined_Product_Description__c` | `name` | OK | Line description. Locked once delivered/invoiced. |
| `Quantity` | `product_uom_qty` | OK | Cannot go below `qty_delivered`. |
| `Total_Billable_Amount__c` | `price_unit` | OK | **Per unit**, not a line total: SF's own formula `Total_Billable_Amount_Quantity__c = Total_Billable_Amount__c * Quantity` proves it. Map straight to `price_unit`. Locked once `qty_invoiced > 0`. |
| `Discount` | — | **Not in SF** | No discount field exists on OpportunityLineItem (or it's hidden from this user). `discount` stays 0; the billable amount is already net. |
| `Year_of_Study__c` | `year_of_study` | EXISTS (lb) | Picklist in SF (incl. ranges like `2023-2024`); Char in Odoo is fine. |
| `Country_Region__c` | `country_region` | EXISTS (lb) | Picklist of 90 market names ("Britain", "Euro 5", "Global"…). These are markets, not countries, so Char is correct; don't map to `res.country`. |
| `Version__c` | `version` | EXISTS (lb) | |
| `Study_Notes_Additional_Description__c` | `item_description` | EXISTS (lb) | Copied to each deliverable by the module. |
| `Data_Vendor_netnew__c` | `sf_data_vendor` | NEW | Picklist (28 vendors). Deliverable already has `data_vendor`; add to line and copy down. |
| `Coverage_Period__c` | `sf_coverage_period` | NEW | Same pattern (deliverable has `coverage_period`). |
| `StudyType__c` | `sf_study_type` | NEW | Picklist (11 values). Same pattern (deliverable has `study_type`). |
| `Study_Name_Formula__c` | `sf_study_name` | NEW | Read-only SF formula (`Study_Name_New__r.Name__c`). Same pattern (deliverable has `study_name`). |
| `Wave_Period__c` | `sf_wave_period` | NEW | Copied to deliverable `wave_label`. |
| `Up_Sell__c` | `sf_is_upsell` | NEW | Copied to deliverable `is_upsell`. |
| `On_Hold__c` | `sf_on_hold` | NEW | Drives deliverable `dispatch_status = 'on_hold'`. |
| `Delivery__c` | `sf_delivery` | NEW — **resolved** | Picklist: Classic, Halo, Both. Same values as the manual Odoo field `x_data_version` ("V10/V5 Data"), so that's what it was for. Add a proper Selection `sf_delivery` (classic/halo/both) and retire `x_data_version`. |
| `Term_Start_Date__c` / `Term_End_Date__c` | `sf_term_start_date` / `sf_term_end_date` | NEW | |
| `Billing_Schedule__c` | `sf_billing_schedule` | NEW / DECIDE | Picklist (14 values: 100% Upfront, Monthly - 1 Year, Quarterly in advance, Quarterly in arrears (4 fiscal variants), As delivered, PSA, Custom…). Default `Prepopulated on Save` is a placeholder → treat as not set. Stored as-is for now. If Odoo should *generate* invoices from it, that's a separate feature (Odoo Community has no billing-schedule engine). Locked once invoiced. |
| `Information_Only__c` | `sf_information_only` | NEW — **resolved** | Checkbox in SF. SF also keeps separate `Info_Only_Dashboard_sub_total__c` amounts, so these are real product lines with a value that isn't billed, not text notes. Store the flag; whether the line gets `price_unit = 0` in Odoo is a finance question. |

---

## 4. Answers to open questions in the requirements doc

### 4.1 Is the billing entity locked on the customer like in NetSuite?
No. In Odoo, `res.partner.company_id` is optional. If it's left **blank**, the
customer is shared across all companies, and sales orders and invoices can be
issued from any of them. That removes the NetSuite "duplicate the
customer to move subsidiaries" problem.

Recommendation: leave `company_id` blank on customers and put the billing entity on
the sales order only. The SF account-level `mTab_Billing_Entity__c` then just sets
the default, stored in `sf_default_billing_entity_id`.

Caveat: some properties are per-company in Odoo (receivable account, payment
terms, pricelist, fiscal position; the `company_dependent` fields in the export).
The connector must write those in the context of the order's company.

Salesforce only has two billing entities: `mTab LLC` and `G.A.M.M.A`. Each needs
to map to one Odoo company. **Open:** which Odoo `res.company` record is each one?

### 4.2 Currency on the customer
Odoo has no settable currency on a partner. Currency comes from the pricelist on
the order. The account-level `CurrencyIsoCode` therefore sets the partner's
default pricelist (one pricelist per currency), and the order-level currency is
enforced by choosing a pricelist in that currency.

### 4.3 Which opportunities sync, and change detection
The award-won stage is exactly `07: Award Won / Contract signed`. The full stage
list also has `Closed` after it, which must keep syncing updates but not create
new orders.

**Finding on the current Celigo filter:** the Opportunity has a
`Send_to_NetSuite__c` checkbox and a `celigo_sfnsio__Skip_Export_To_NetSuite__c`
checkbox. That suggests the existing flow is triggered by a checkbox, not
purely by stage, which would explain the doubt in the requirements doc. Run the
query in §7 to see which stages the checked opportunities are actually in.

Proposed create filter: `StageName = '07: Award Won / Contract signed'`, and
optionally a new `Send_to_Odoo__c` checkbox if finance wants a manual gate.
Accounts sync only when one of their opportunities qualifies.

Change detection, SF side: query with `SystemModstamp > :last_successful_run`
per object. The connector stores the high-water mark per object.
Odoo side (invoice sync back): `write_date > :last_run` on `account.move`.

### 4.4 Line deletion / locked-field rules
All guard data is available in Odoo:
`qty_delivered`, `qty_invoiced`, `invoice_lines` on `sale.order.line`, and
`deliverable_delivered_count` from the deliverables module. The rules map as:

| Rule | Check |
|---|---|
| Qty can't drop below fulfilled | new qty ≥ `qty_delivered` (and ≥ `deliverable_delivered_count`) |
| Price locked after any invoicing | `qty_invoiced > 0` → reject `price_unit`/`discount` change |
| Billing schedule locked after invoicing | `qty_invoiced > 0` → reject `sf_billing_schedule` change |
| Description locked if delivered/invoiced | `qty_delivered > 0 or qty_invoiced > 0` → reject `name` change |
| Line removed in SF, fulfilled | `qty_delivered > 0` → error, stop processing the order |
| Line removed in SF, invoiced only | set `product_uom_qty = 0`, keep the line |
| Line removed in SF, untouched | delete the line |
| Locked header fields changed | billing entity, account, currency, award-won date → error |

The Limebox diagram is still needed to confirm the order of these checks.

### 4.5 Fulfillment
Professional services: marked fulfilled in the deliverables app (`is_fulfilled`
on `lb.item.deliverable`). Subscriptions: fulfilled when invoiced. The connector
reads both; nothing new is needed in Odoo.

---

## 5. Odoo → Salesforce (invoices)

| Odoo field (`account.move`) | Purpose |
|---|---|
| `name` | Invoice number |
| `invoice_date`, `invoice_date_due` | Dates |
| `amount_untaxed`, `amount_tax`, `amount_total`, `amount_residual` | Amounts |
| `payment_state` | Paid / partial / not paid |
| `invoice_pdf_report_file` | PDF to attach in SF (as a ContentVersion on the Opportunity) |
| `invoice_line_ids.sale_line_ids` | Links invoice lines back to SO lines → `sf_line_id` |

The source doc's invoice section is cut off mid-sentence ("send to …"). Need the
rest: where the PDF goes and which SF object receives the invoice record.

---

## 6. New fields summary (`sf_connector` Odoo module)

- `res.partner`: `sf_account_id`, `sf_legal_entity_name`, `sf_billing_distribution_method`, `sf_default_billing_entity_id`
- `sale.order`: `sf_opportunity_id`, `sf_revenue_allocation_company_id`, `sf_award_won_date`, `sf_term_start_date`, `sf_opportunity_type`, `sf_description`, `sf_invoice_notes`, `sf_invoice_additional_info`, `sf_billing_distribution_method`
- `sale.order.line`: `sf_line_id`, `sf_data_vendor`, `sf_coverage_period`, `sf_study_type`, `sf_study_name`, `sf_wave_period`, `sf_is_upsell`, `sf_on_hold`, `sf_term_start_date`, `sf_term_end_date`, `sf_billing_schedule`, `sf_delivery`, `sf_information_only`

**Changes needed in the installed v19.0.1.0.0 module** (from the SF metadata):
1. `sale.order.sf_revenue_allocation_company_id`: change relation from `res.company` to `res.partner`.
2. Add `sale.order.sf_invoice_notes` (Text, internal).
3. Add `sale.order.line.sf_delivery` (Selection: classic / halo / both).
4. Add `sale.order.line.sf_information_only` (Boolean).
5. Optional: make `sale.order.sf_opportunity_type` a Selection (new / up_sell / cross_sell / renewal).

Fields use an `sf_` prefix (Odoo reserves `x_` for fields created through the UI). They go in a real addon (version-controlled, in this repo) rather than being
created as manual fields in Settings, so they deploy identically to sandbox and
production.

---

## 7. Remaining open items

| # | Item | Who |
|---|---|---|
| 1 | Which Odoo company is `mTab LLC`, and which is `G.A.M.M.A`? | Jason / finance |
| 2 | Do the 12 SF payment terms exist in Odoo with matching names? | Jason |
| 3 | Pricebook → pricelist map, one pricelist per currency (GBP, CNY, EUR, JPY, USD) | Jason / finance |
| 4 | Do Odoo products carry the SF `ProductCode` in `default_code`? | Jason |
| 5 | Should information-only lines be priced at 0 in Odoo? | Finance |
| 6 | Fax: drop or keep? | Finance |
| 7 | `VAT_Tax_Number__c` on the opportunity vs the account VAT: can they differ? | Finance |
| 8 | Billing schedule: store only, or should Odoo generate invoices from it? | Finance |
| 9 | Limebox line-removal diagram; end of the invoice section of the requirements doc | Requirements author |
| 10 | Admin: integration user, OAuth app, `Odoo_Id__c` / `Odoo_Record__c` / `Odoo_Sync_Error__c` fields, field locks | SF admin |

Diagnostic queries (run with `--target-org mtab-sandbox`):

```
sf data query --query "SELECT StageName, COUNT(Id) FROM Opportunity WHERE Send_to_NetSuite__c = true GROUP BY StageName"
sf data query --query "SELECT BillingCountry, COUNT(Id) FROM Account GROUP BY BillingCountry ORDER BY COUNT(Id) DESC"
```
