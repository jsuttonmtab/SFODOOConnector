# Salesforce → Odoo 19 Field Mapping

Source: SFDC field list (requirements doc) and `odoo_fields.json` exported from
`OdooSandbox19` on mpl-odoo (server 19.0-20260928).

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
| `Fax` | — | DECIDE | Odoo 19 has no fax field. Drop it, or add `sf_fax`. |
| `Website` | `website` | OK | |
| `Description` | `comment` | OK | Odoo field is HTML; plain text is escaped. |
| `VAT_Number_Text__c` | `vat` | OK | Odoo validates VAT format per country if `base_vat` is installed. |
| `mTab_Billing_Entity__c` | `company_id` | LOOKUP / DECIDE | See §4.1. |
| `Account_Manager__r.Email` | `user_id` | LOOKUP | `res.users` by login/email. Unmatched → leave blank and log a warning. |
| `CurrencyIsoCode` | `property_product_pricelist` | LOOKUP / DECIDE | Partners have no writable currency (`currency_id` is computed, read-only). Currency is carried by the pricelist. See §4.2. |
| `Billing_Distribution_Method__c` | `sf_billing_distribution_method` | NEW | Could alternatively drive stock `invoice_sending_method` (manual/email/post). Need SF picklist values. |
| `Parent.Name` | `parent_id` | LOOKUP | Resolve by parent's `sf_account_id` rather than name (names aren't unique). Requires SF `ParentId`, not just `Parent.Name`. |
| `BillingAddress1` / `BillingCity` / `BillingState` / `BillingCountry` / `BillingPostalCode` | `street` / `city` / `state_id` / `country_id` / `zip` | OK + LOOKUP | Country by ISO code or name → `res.country`; state by code within country → `res.country.state`. |
| `ShippingAddress1` … `ShippingPostalCode` | child `res.partner` with `type = 'delivery'` | OK + LOOKUP | Odoo stores alternate addresses as child contacts. Created only when shipping differs from billing. |

**Write-back to SFDC:** Odoo partner ID and record URL → two new SF fields on
Account (e.g. `Odoo_Customer_ID__c`, `Odoo_Customer_URL__c`).

---

## 2. Opportunity → `sale.order`

| SFDC field | Odoo field | Status | Notes |
|---|---|---|---|
| `Id` | `sf_opportunity_id` | NEW | Unique, indexed. Upsert key. |
| `Account.Id` | `partner_id` | LOOKUP | Via `sf_account_id`. Locked after create. |
| `Name` | `origin` | OK | `name` stays Odoo's own SO sequence (S000xx). |
| `PO_Number__c` | `client_order_ref` | OK | Stock "Customer Reference"; prints on invoices. |
| `mTab_Billing_Entity__c` | `company_id` | LOOKUP | Locked after create. |
| `Revenue_allocation_company_ID__r.Name` | `sf_revenue_allocation_company_id` | NEW | many2one `res.company`. |
| `CurrencyIsoCode` | via `pricelist_id` | LOOKUP | SO `currency_id` is read-only and comes from the pricelist. Locked after create. |
| `Pricebook2Id` | `pricelist_id` | LOOKUP / DECIDE | Needs a Pricebook → pricelist map. Pricelist currency must match `CurrencyIsoCode`. |
| `Payment_Terms__c` | `payment_term_id` | LOOKUP | Need SF picklist → `account.payment.term` map. |
| `Owner.Email` | `user_id` | LOOKUP | `res.users`. |
| `Date_Award_Won0__c` | `sf_award_won_date` | NEW | Locked after create. `date_order` gets set to this too. |
| `Term_Start_Date__c` | `sf_term_start_date` | NEW | |
| `Type` | `sf_opportunity_type` | NEW | Need SF picklist values. |
| `Description` | `sf_description` | NEW | Internal; not printed. |
| `Invoice_Notes__c` | `note` | OK | Prints on quotation/invoice as T&C block. Confirm this is the intended placement. |
| `Additional_information_to_invoice__c` | `sf_invoice_additional_info` | NEW | Needs an invoice report tweak to print. |
| `VAT_Tax_Number__c` | — | DECIDE | Odoo takes VAT from the invoice partner. If the order VAT can differ from the account VAT, it must go on the invoice address partner. |
| `Billing_Distribution_Method__c` | `sf_billing_distribution_method` | NEW | Same values as Account. |
| `Parent_Account__r.Name` | — | OK (derived) | Already implied by `partner_id.parent_id`. Not stored. |
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
| `rapidproducts__ProductName__c` | `product_id` | LOOKUP | Match `product.product` by name or `default_code`. Unmatched → error, line not created. |
| `Combined_Product_Description__c` | `name` | OK | Line description. Locked once delivered/invoiced. |
| `Quantity` | `product_uom_qty` | OK | Cannot go below `qty_delivered`. |
| `Total_Billable_Amount__c` | `price_unit` | OK (computed) | `price_unit = Total_Billable_Amount__c / Quantity`, or set price and discount separately. Need to confirm whether the total is pre- or post-discount. Locked once `qty_invoiced > 0`. |
| `Discount` | `discount` | OK | Odoo stores a percentage. If SF stores an amount, convert. |
| `Year_of_Study__c` | `year_of_study` | EXISTS (lb) | |
| `Country_Region__c` | `country_region` | EXISTS (lb) | |
| `Version__c` | `version` | EXISTS (lb) | |
| `Study_Notes_Additional_Description__c` | `item_description` | EXISTS (lb) | Copied to each deliverable by the module. |
| `Data_Vendor_netnew__c` | `sf_data_vendor` | NEW | Deliverable already has `data_vendor`; add to line and copy down. |
| `Coverage_Period__c` | `sf_coverage_period` | NEW | Same pattern (deliverable has `coverage_period`). |
| `StudyType__c` | `sf_study_type` | NEW | Same (deliverable has `study_type`). |
| `Study_Name_Formula__c` | `sf_study_name` | NEW | Same (deliverable has `study_name`). |
| `Wave_Period__c` | `sf_wave_period` | NEW | Copied to deliverable `wave_label`. |
| `Up_Sell__c` | `sf_is_upsell` | NEW | Copied to deliverable `is_upsell`. |
| `On_Hold__c` | `sf_on_hold` | NEW | Drives deliverable `dispatch_status = 'on_hold'`. |
| `Delivery__c` | — | DECIDE | Unclear meaning (delivery method? date? count?). Need the SF field type. |
| `Term_Start_Date__c` / `Term_End_Date__c` | `sf_term_start_date` / `sf_term_end_date` | NEW | |
| `Billing_Schedule__c` | `sf_billing_schedule` | NEW / DECIDE | Stored as-is for now. If Odoo should *generate* invoices from it, that's a separate feature (Odoo Community has no subscription/billing-schedule engine). Locked once invoiced. |
| `Information_Only__c` | `display_type = 'line_note'` | DECIDE | If "information only" means a non-billable text line, it maps to an Odoo note line (no product, no price). Otherwise a boolean `sf_information_only`. |

---

## 4. Answers to open questions in the requirements doc

### 4.1 Is the billing entity locked on the customer like in NetSuite?
No. In Odoo, `res.partner.company_id` is optional. If it's left **blank**, the
customer is shared across all companies (MUS, MPL, MUK), and sales orders and
invoices can be issued from any of them. That removes the NetSuite "duplicate the
customer to move subsidiaries" problem.

Recommendation: leave `company_id` blank on customers and put the billing entity on
the sales order only. The SF account-level `mTab_Billing_Entity__c` then just sets
the default, stored in `sf_default_billing_entity_id`.

Caveat: some properties are per-company in Odoo (receivable account, payment
terms, pricelist, fiscal position; the `company_dependent` fields in the export).
The connector must write those in the context of the order's company.

### 4.2 Currency on the customer
Odoo has no settable currency on a partner. Currency comes from the pricelist on
the order. The account-level `CurrencyIsoCode` therefore sets the partner's
default pricelist (one pricelist per currency), and the order-level currency is
enforced by choosing a pricelist in that currency.

### 4.3 Change detection (only sync changed records)
SF side: query with `SystemModstamp > :last_successful_run` per object, plus
Opportunity stage filter. The connector stores the high-water mark per object.
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
- `sale.order`: `sf_opportunity_id`, `sf_revenue_allocation_company_id`, `sf_award_won_date`, `sf_term_start_date`, `sf_opportunity_type`, `sf_description`, `sf_invoice_additional_info`, `sf_billing_distribution_method`
- `sale.order.line`: `sf_line_id`, `sf_data_vendor`, `sf_coverage_period`, `sf_study_type`, `sf_study_name`, `sf_wave_period`, `sf_is_upsell`, `sf_on_hold`, `sf_term_start_date`, `sf_term_end_date`, `sf_billing_schedule`

Fields use an `sf_` prefix (Odoo reserves `x_` for fields created through the UI). They go in a real addon (version-controlled, in this repo) rather than being
created as manual fields in Settings, so they deploy identically to sandbox and
production.
