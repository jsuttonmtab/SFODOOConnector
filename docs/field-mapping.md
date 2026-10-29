# Salesforce → Odoo 19 Field Mapping

Source: SFDC field list (requirements doc), `odoo_fields.json` exported from
`OdooSandbox19` on mpl-odoo (server 19.0-20260928), and `sf sobject describe`
output for Account, Opportunity and OpportunityLineItem from the `mtab--staging`
sandbox (2026-10-12).

Finance/CS answers received 2026-10-14 are folded in throughout; see §8 for the decision log.

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
| `Fax` | — | DROPPED | Not used at mTab (Finance, 2026-10-14). |
| `Website` | `website` | OK | |
| `Description` | `comment` | OK | Odoo field is HTML; plain text is escaped. |
| `VAT_Number_Text__c` | `vat` | OK | Read-only SF formula over `VAT_Number__c` (textarea). Cleaned before writing, see *VAT handling* below. If Odoo rejects the VAT, the SF value is kept in `sf_vat_rejected`. |
| `mTab_Billing_Entity__c` | `sf_default_billing_entity_id` | LOOKUP | Picklist: `mTab LLC` (US subsidiary), `G.A.M.M.A` (UK subsidiary, "Gamma"). Mapped to Odoo companies through connector config. See §4.1. |
| `Account_Manager__r.Email` | `user_id` | LOOKUP | `res.users` by login/email. Unmatched → leave blank and log a warning. |
| `CurrencyIsoCode` | `property_product_pricelist` | LOOKUP | Picklist: GBP, CNY, EUR, JPY, USD. Partners have no writable currency (`currency_id` is computed, read-only). Currency is carried by the pricelist; the connector uses one pricelist per currency. See §4.2. |
| `Billing_Distribution_Method__c` | `sf_billing_distribution_method` | NEW | Free text in SF (string 100), not a picklist. Stored as Char. |
| `ParentId` | `sf_parent_account_id` | LOOKUP | **Not `parent_id`** (decided 2026-10-15). Odoo's `parent_id` copies the parent's address into contact children and pushes parent address changes down, which scrambled addresses and caused cross-record VAT errors. Resolve by the parent's `sf_account_id`, not by name (names aren't unique). Parents are synced as companies even without their own stage-07 opportunity: the connector walks the ParentId chain upwards. Parents that are test-mode, on the test-account list or missing in SF are not synced, and the link stays blank. Delivery-address children still use `parent_id` with `type = 'delivery'`. |
| `BillingStreet` / `BillingCity` / `BillingState` / `BillingCountry` / `BillingPostalCode` | `street` / `city` / `state_id` / `country_id` / `zip` | OK + LOOKUP | SF state/country picklists are **not** enabled (no `BillingCountryCode`), so country and state are free text. Match with the rules in §4.1a. Unmatched → leave blank and log. Less critical than it looked: the invoice address comes from the opportunity, which has its own country (§2). |
| `ShippingStreet` … `ShippingPostalCode` | child `res.partner` with `type = 'delivery'` | OK + LOOKUP | Odoo stores alternate addresses as child contacts. Created only when shipping differs from billing. Same free-text country caveat. |

#### VAT handling (2026-10-15)

Before writing, `VAT_Number_Text__c` is cleaned:

1. The label before the first colon is removed (`NIP:`, `VAT:` and similar).
2. Spaces, dots and hyphens are removed, and the result is upper-cased.
3. If the value does not start with the billing country's ISO code, the code is added. Example: `NIP: 123.456.789` with Polish billing becomes `PL123456789`. A blank value writes no VAT.

The same compact form is used when comparing with Odoo, so a VAT that only differs in spacing or label is not an update.

**If Odoo rejects the VAT** (HTTP 422 mentioning VAT), the record is saved without VAT, and:

- the Salesforce value is stored in `sf_vat_rejected` (readonly, Salesforce tab in Odoo);
- a grouped "VAT rejected" warning is printed, with the SF value.

On later runs, while the SF VAT still equals `sf_vat_rejected`, the connector does not write VAT again, and the record counts as unchanged if nothing else differs. Each run prints an info line with the number of such accounts. When the SF VAT changes, it is tried again. If Odoo accepts it, `sf_vat_rejected` is cleared.

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
| `mTab_Billing_Entity__c` | `company_id` | LOOKUP | `mTab LLC` → US company, `G.A.M.M.A` → UK company (via connector config). The placeholder value `This will prepopulate on save` means not set → reject the order. Locked after create. |
| `Revenue_allocation_company_ID__c` | `sf_revenue_allocation_company_id` | NEW — **fix type** | In SF this is a lookup to **Account**, not to an mTab entity. The Odoo field must be many2one `res.partner` (resolved via `sf_account_id`), not `res.company`. |
| `CurrencyIsoCode` | via `pricelist_id` | LOOKUP | GBP, CNY, EUR, JPY, USD. SO `currency_id` is read-only and comes from the pricelist. Locked after create. |
| `Pricebook2Id` | — | NOT SYNCED | Price books stay in SF only (Finance, 2026-10-14). The connector picks the Odoo pricelist from `CurrencyIsoCode` instead. |
| `Payment_Terms__c` | `payment_term_id` | LOOKUP | 12 values: Net 30, Net 30 EOM, Net 45, Net 45 PSA, Net 55, Net 60, Net 60 EOM, Net 65, Net 75, Net 90, Net 120, Net 180. Synced by name. "Net X" = due X days after invoice date; "Net X EOM" = due X days after the end of the invoice month. The `sf_connector` module ships all 12 as data. "Net 45 PSA": meaning to confirm, treated as Net 45 for now. Placeholder `This will prepopulate on save` → reject. |
| `OwnerId` → `Owner.Email` | `user_id` | LOOKUP | `res.users` by email. |
| `Date_Award_Won_Date__c` | `sf_award_won_date` | NEW | **Changed from `Date_Award_Won0__c`**, which is a text formula over a text field. `Date_Award_Won_Date__c` is a real date field. Locked after create. `date_order` gets set to this too. |
| `Term_Start_Date__c` | `sf_term_start_date` | NEW | |
| `Type` | `sf_opportunity_type` | NEW | Picklist: New, Up-Sell, Cross-Sell, Renewal. Can become a Selection. |
| `Description` | `sf_description` | NEW | Internal; not printed. |
| `Invoice_Notes__c` | `sf_invoice_notes` | NEW (confirmed) | Internal instructions from CS to Finance on how to invoice, often for "Custom" billing schedules. Multi-line text; never printed. |
| `Additional_information_to_invoice__c` | `note` + `sf_invoice_additional_info` | OK (confirmed) | Goes in the Notes section of the invoice. Writing it to the SO `note` field makes Odoo copy it to the invoice notes automatically, so no report change is needed. Raw copy kept in `sf_invoice_additional_info`. |
| `VAT_Tax_Number__c` | `sf_vat_number` | NEW | Inherited from the account in SF and (almost) never differs. Keep the NetSuite workflow: store it on the order. Odoo invoices use the customer's `vat`; the connector logs a warning if the two differ. |
| `Billing_Distribution_Method__c` | `sf_billing_distribution_method` | NEW | Free text, same as Account. |
| `Parent_Account__c` | — | OK (derived) | Lookup to Account. Already implied by `partner_id.sf_parent_account_id`. Not stored. |
| `Billing_Street__c` / `Billing_City__c` / `Billing_State_Province__c` / `Billing_Zip_Postal_Code__c` / **`Billing_Country__c`** | `partner_invoice_id` | LOOKUP | Find-or-create a child partner `type = 'invoice'` under the customer with this address. **`Billing_Country__c` was missing from the requirements doc** but exists on the opportunity; it's the country used for invoicing. Matched with §4.1a. SF already has a formula (`Billing_Country_Formula__c`) normalising USA/U.S.A/UK, which confirms the same spelling problem. |
| `Billing_Address__c` | — | — | Text duplicate of the fields above. |
| `StageName` | `sf_stage` | NEW | Current SF stage, stored for visibility and for the reversal rule in §4.6. |
| (none: Odoo) | `project_id` | SET | Set to the `DS deliverables` project (`DELIVERABLES_PROJECT`) so the mTab Delivery Tracker lists the order's deliverables (it filters on project name). Set on create. On update, set only when the order has no project, so a project chosen by hand is never overwritten; this also backfills existing orders. Allowed on confirmed orders. The project must be active and billable. An order whose company differs from the project's company is left without a project and warned. |

**Status on create:** `state = 'draft'` (Quotation). Finance clicks *Confirm* to
move it to `sale`, which releases it to the deliverables app. Agreed by Finance
for testing (2026-10-14).

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

**Decision (2026-10-13): keep the `x_` fields.** They stay in Odoo unchanged.
The connector writes to the module fields only and does not touch the `x_` fields.
The "Two fields have the same label: Country" warning on module upgrades comes
from `x_country` and is harmless.

| SFDC field | Odoo field | Status | Notes |
|---|---|---|---|
| `Id` | `sf_line_id` | NEW | Upsert key per line. Also copied to `lb.item.deliverable.sf_opportunity_line_id` (already exists there). |
| `celigo_sfnsio__NetSuite_Line_Id__c` | — | NOT SYNCED | Legacy Celigo key, replaced by `sf_line_id`. Revisit only if NetSuite orders are migrated. |
| `Product2Id` (→ `Product2.ProductCode`) | `product_id` | LOOKUP | Match on product code → `default_code`, which is more reliable than `rapidproducts__ProductName__c` (free text). Fall back to name. Unmatched → error, line not created. Requires Odoo products to carry the SF product codes. |
| `Combined_Product_Description__c` | `name` | OK | Line description. Locked once delivered/invoiced. |
| `Quantity` | `product_uom_qty` | OK | Cannot go below `qty_delivered`. |
| `Total_Billable_Amount__c` | `price_unit` | OK | Ignored (price 0) on information-only lines. **Per unit**, not a line total: SF's own formula `Total_Billable_Amount_Quantity__c = Total_Billable_Amount__c * Quantity` proves it. Map straight to `price_unit`. Locked once `qty_invoiced > 0`. |
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
| `On_Hold__c` | `sf_on_hold` | NEW | Must update the deliverable report immediately so DS doesn't release the data (CS, 2026-10-14). The line and its deliverables stay listed but show as on hold; clearing the flag releases them. See §4.7 for sync frequency. |
| `Delivery__c` | `sf_delivery` | NEW — **resolved** | Picklist: Classic, Halo, Both. Same values as the manual Odoo field `x_data_version` ("V10/V5 Data"), so that's what it was for. Add a proper Selection `sf_delivery` (classic/halo/both). `x_data_version` is kept (see decision above). |
| `Term_Start_Date__c` / `Term_End_Date__c` | `sf_term_start_date` / `sf_term_end_date` | NEW | |
| `Billing_Schedule__c` | `sf_billing_schedule` | NEW (store only) | Picklist (14 values: 100% Upfront, Monthly - 1 Year, Quarterly in advance, Quarterly in arrears (4 fiscal variants), As delivered, PSA, Custom…). Default `Prepopulated on Save` is a placeholder → treat as not set. Stored as-is. Finance wants Odoo eventually to prompt them when an invoice is due (by delivery or by date), but that is out of scope for integration testing. Locked once invoiced. |
| `Information_Only__c` | `sf_information_only` | NEW (decided) | Info-only lines are the deliverables that make up a "Data & Analytic Services Subscription"; the subscription line is what's billed. Sync them as real lines with **`price_unit = 0`** so they feed the DS deliverable report. They are invoiced as normal (at zero) so orders don't sit at "to invoice", but must be **hidden on the invoice PDF**. NetSuite did this with a custom template; in Odoo it's an invoice report change, deferred to the implementation phase. |

---

## 4. Answers to open questions in the requirements doc

### 4.1 Billing entities and shared customers
`mTab LLC` is the US subsidiary and `G.A.M.M.A` ("Gamma") the UK subsidiary.
These and several more entities will be created in Odoo during the
implementation. The connector maps the SF picklist value to an Odoo company by
name through a config table, so the real companies can be plugged in later. For
sandbox testing, two test companies with these names are enough.

**Decision for testing (2026-10-14):** customers are shared (`res.partner.company_id`
blank), so either entity can sell to and invoice them. Finance noted the risk of
invoicing from the wrong entity by mistake; this is limited because each order's
company is set from the SF billing entity, not chosen by hand. Revisit before
go-live.

Caveat: some properties are per-company in Odoo (receivable account, payment
terms, pricelist, fiscal position; the `company_dependent` fields in the export).
The connector must write those in the context of the order's company.

### 4.1a Country matching
Sandbox data (2026-10-12): about 2,200 accounts. 1,043 have no `BillingCountry`;
the rest use 51 distinct spellings. Matching rule, in order:

1. Case-insensitive match on `res.country.name` (handles `GERMANY`, `PHILIPPINES`)
2. Case-insensitive match on `res.country.code` (handles `US`)
3. Alias table below
4. No match → leave `country_id` blank, log a warning, still create the partner

| SF value | ISO code |
|---|---|
| USA, US, United States | US |
| UK, United Kingdom | GB |
| UAE, U.A.E., United Arab Emirates | AE |
| Korea, South Korea, Republic of Korea | KR |
| China, Peoples Republic of China, PRC (China) | CN |
| The Netherlands | NL |
| Russia | RU |
| Turkey | TR |
| Czech Republic | CZ |
| Vietnam | VN |
| Taiwan | TW |

The last five are in the table because Odoo's display names may differ
(e.g. "Türkiye", "Czechia", "Russian Federation"). Matching by code avoids that.

Michael Martin built an equivalent alias list in Celigo for the NetSuite
integration. **Reuse it** to extend this table rather than starting over.

A missing country matters for invoicing, since Odoo's fiscal positions and taxes
depend on it. In the sandbox, 0 accounts with a stage-07 opportunity lack a
country, but 79 accounts with only `Closed` opportunities do. They matter only if
historical orders are migrated or those customers win new deals. The connector
should report any customer it syncs without a country.

Since the opportunity carries its own `Billing_Country__c` (§2), the order's
invoice address is what drives taxes. The check that matters is open orders
whose opportunity has no `Billing_Country__c` (query in §7).

### 4.2 Currency on the customer
Odoo has no settable currency on a partner. Currency comes from the pricelist on
the order. SF price books are **not** synced (Finance, 2026-10-14). Instead the
connector keeps one Odoo pricelist per currency (GBP, CNY, EUR, JPY, USD) purely to
carry the currency; prices always come from SF. The account-level
`CurrencyIsoCode` sets the partner's default pricelist, and each order uses the
pricelist matching the opportunity currency.

### 4.3 Which opportunities sync, and change detection
The award-won stage is exactly `07: Award Won / Contract signed`. The full stage
list also has `Closed` after it, which must keep syncing updates but not create
new orders.

**Finding on the current Celigo filter:** the Opportunity has a
`Send_to_NetSuite__c` checkbox and a `celigo_sfnsio__Skip_Export_To_NetSuite__c`
checkbox. In the sandbox, no opportunity currently has `Send_to_NetSuite__c`
checked, so it's either cleared after export or unused.

Opportunities that actually reached NetSuite (have `celigo_sfnsio__NetSuite_Id__c`),
by stage: Closed 1,564; 07 Award Won 336; Award Lost 8; 02 Demo 1; 01 Discovery 2.
So Celigo effectively syncs at award-won, and won deals later move to `Closed`.
The 11 synced deals now in Award Lost or early stages need review: NetSuite has
orders for deals Salesforce no longer shows as won.

CS moves an opportunity to `Closed` once the contract is **fully delivered**
(Finance, 2026-10-14). So the 1,564 `Closed` ones are finished work; the open
orders are the 336 in stage 07.

**Rule:** create orders only for stage 07. `Closed` opportunities that already
exist in Odoo keep receiving updates; `Closed` opportunities not in Odoo are
ignored. Historical NetSuite orders are not migrated during testing. For go-live,
the proposal is to load the open (stage 07) orders only (§7).

**Test records:** test opportunities have reached NetSuite in the past. The
connector skips records flagged `celigo_sfnsio__Test_Mode_Record__c` and accounts
on a configurable test-account list (e.g. TestAccount, Celigo Company).

Proposed create filter: `StageName = '07: Award Won / Contract signed'`, and
optionally a new `Send_to_Odoo__c` checkbox if finance wants a manual gate.
Accounts sync only when one of their opportunities qualifies.

Change detection, SF side: query with `SystemModstamp > :last_successful_run`
per object. The connector stores the high-water mark per object.
Odoo side (invoice sync back): `write_date > :last_run` on `account.move`.

### 4.4 Line deletion / locked-field rules

#### Removed lines (Finance, 2026-10-14; supersedes the Limebox diagram)

The Limebox diagram (`docs/limebox-line-removal.png`) was the design proposal.
Current NetSuite practice, which Finance wants kept, is simpler:

| Situation | Connector action |
|---|---|
| Line removed in SF and **fulfilled in any quantity** (`qty_delivered > 0` or `deliverable_delivered_count > 0`) | **Fail**: apply no changes to the order, write the error to `Odoo_Sync_Error__c` in SF. SF normally blocks this deletion already; this is the safety net. |
| Any other removed line (invoiced or not) | **Keep the line, set `product_uom_qty = 0`**, set `sf_removed_in_sf = True`. It stays as a change log, and the deliverables app picks up the quantity change. |

**No automatic un-billing.** Over-invoicing only happens with scheduled billing
(e.g. quarterly), where a line is partly invoiced and then removed. NetSuite does
**not** edit posted invoices (correcting an earlier assumption in this doc); it
bills a negative quantity on the next scheduled invoice. Odoo behaves the same way
by default: once ordered < invoiced, the next invoice created from the order
includes the negative quantity. Nothing to build for testing.

**Detecting removed lines:** deleted OpportunityLineItems disappear from normal
queries. On every opportunity update, the connector compares the Odoo order's
`sf_line_id`s with the current SF lines; any Odoo line whose SF line is gone is
treated as removed.

#### Other locked-field rules

| Rule | Check |
|---|---|
| Qty can't drop below fulfilled | new qty ≥ `qty_delivered` (and ≥ `deliverable_delivered_count`) |
| Price locked after any invoicing | `qty_invoiced > 0` → reject `price_unit` change |
| Billing schedule locked after invoicing | `qty_invoiced > 0` → reject `sf_billing_schedule` change |
| Description locked if delivered/invoiced | `qty_delivered > 0 or qty_invoiced > 0` → reject `name` change |
| Locked header fields changed | billing entity, account, currency, award-won date → error |

"Reject" means: don't apply that field, write the error back to SF, continue
with the rest of the order.

### 4.5 Fulfillment
Professional services: marked fulfilled in the deliverables app (`is_fulfilled`
on `lb.item.deliverable`). Subscriptions: fulfilled when invoiced. The connector
reads both; nothing new is needed in Odoo.

### 4.6 Opportunity moved out of award-won after sync
Not handled by the current Celigo integration. In the sandbox, 8 synced
opportunities were later moved to Award Lost. CS reviewed them (2026-10-14):
genuine cancellations (deal dropped, funded from a main PO, Russia cut-off,
order merged into another opportunity) and a few stage misclicks. None needs
action now.

Finance prefers handling this in Odoo rather than locking SF. **Rule:**

- If a synced opportunity moves from stage 07 / `Closed` to any other stage, the
  connector does **not** change or cancel the Odoo order.
- A reversal is acted on only when `sf_stage` in Odoo differs from the stage in
  Salesforce. A reversed order whose stage has not changed since the last run is
  counted silently and produces no warning, note or To Do.
- It posts an internal note on the order and creates a **To Do** for the Finance
  user (`FINANCE_ACTIVITY_USER`) to review (cancel, keep, or wait for SF to be
  corrected). The To Do is created with Odoo's standard `activity_schedule`, so
  the connector needs no access to `ir.model`.
- It updates `sf_stage` last. Each step checks Odoo first, so a run that fails
  part-way finishes on the next run: the note is posted once and a To Do is created
  only if no open connector To Do is on the order.
- **One note per reversal.** When an order returns to Award Won, the connector
  posts a marker note ("Salesforce stage back to …"). A reversal note counts as
  already posted only if it comes after the latest marker. So a deal that goes
  07 → 06, back to 07, then 06 again gets a second note. The marker is a chatter
  message because it survives restarts and needs no extra field.
- When an order's lines change, the connector posts one note per run on the order
  listing the Finance/CS changes: product, quantity, unit price, On Hold, removed
  and added lines, in the order currency's format. Nothing is posted when nothing
  changed, and a new order gets no note. The note is posted after the lines are
  written. If that post fails, the change is not repeated, so the note is missing.
- If `FINANCE_ACTIVITY_USER` is unset, not found, archived or a portal user, the
  connector warns once per run and skips the To Do; the note is still posted.
- If `FINANCE_ACTIVITY_USER` is the API user (`ODOO_API_LOGIN`, when set), the
  To Do is created but Odoo sends no assignment email. The connector warns.
- It is written to `Odoo_Sync_Error__c` in SF (§5.1) for as long as the reversal lasts.
- If the opportunity returns to stage 07 / `Closed`, the To Do can be closed by
  Finance; syncing continues normally. The connector does not close it.

Optional, SF admin: a confirmation prompt when changing the stage of an
opportunity that has an Odoo ID, to catch misclicks.

### 4.7 Sync frequency
On Hold must reach the deliverable report "immediately". Polling SF for changed
lines (`SystemModstamp`) every **5 minutes** is the starting point. If that's not
fast enough, SF Change Data Capture can push changes instead; that needs the SF
admin.

---

## 5. Odoo → Salesforce

### 5.1 Write-back: Odoo ids, links, sync errors and line quantities (phase 1)

Implemented in `connector/sf_writeback.py`. It runs at the end of `sync_accounts` and `sync_orders`,
after the Odoo side, so a Salesforce failure cannot stop the Odoo sync. Nothing is written to
Salesforce unless `SF_WRITEBACK=true`. Field names are the org's spelling (`Odoo_ID__c`).

| Object | Field | Value |
|---|---|---|
| Account | `Odoo_ID__c` | `res.partner` id |
| Account | `Odoo_Record__c` | `<ODOO_PUBLIC_URL>/odoo/contacts/<id>` |
| Account | `Odoo_Sync_Error__c` | This run's errors and warnings for the account, one per line. Blank when clean. |
| Account | `Odoo_Last_Sync__c` | UTC time of the write (see the rules below) |
| Opportunity | `Odoo_ID__c` | `sale.order` id. Blank while there is no order. |
| Opportunity | `Odoo_Record__c` | `<ODOO_PUBLIC_URL>/odoo/action-sale.action_orders/<id>` (check the link on the first real run) |
| Opportunity | `Odoo_Sync_Error__c` | This run's messages for the opportunity, including its lines' failures (§4.4), plus the stage-reversal message while the order's opportunity is out of Award Won (§4.6) |
| Opportunity | `Odoo_Last_Sync__c` | As for Account |
| OpportunityLineItem | `Odoo_Line_ID__c` | `sale.order.line` id |
| OpportunityLineItem | `Odoo_Qty_Delivered__c` | `qty_delivered`. For deliverable lines (`deliverable_count > 0`), `deliverable_delivered_count`. |
| OpportunityLineItem | `Odoo_Qty_Invoiced__c` | `qty_invoiced` |
| OpportunityLineItem | `Odoo_Fulfilled__c` | Deliverable lines (professional services): every deliverable delivered. Other lines (subscriptions): fully invoiced (`qty_invoiced >= product_uom_qty`, quantity above 0). |

**Rules**

- Only fields whose Salesforce value differs are written. Blank and null are the same, and quantities
  compare to 2 decimals. A run where nothing differs writes nothing.
- `Odoo_Last_Sync__c` changes only when another field on the same record changes, or the error text
  does. Otherwise a record is not touched every 15 minutes.
- Errors on a line have no field of their own in Salesforce, so they go on the opportunity.
- The stage-reversal message is based on the current state, not on the run that found the reversal.
  The reversal is detected once (`sf_stage` is then updated), so a message raised only in that run
  would clear on the next run and nobody would see it.
- A record with no Odoo record yet (for example a Closed opportunity that was never synced) keeps its
  `Odoo_ID__c` and `Odoo_Record__c` blank. Its error text is still written.
- Writes go 200 records per call (Composite sObject Collections, `allOrNone` off). A record Salesforce
  rejects is counted under its error and the rest still go. Failures are grouped by error in the log.
- Before writing to an object, every field above is checked: it must exist, have the expected type and
  be updateable by the integration user. If any does not, that object is skipped and the problem is logged.
  A dry run still prints the changes and says a real run would stop.
- A real write is refused unless the authenticated org Id equals `SF_EXPECTED_ORG_ID` and `ODOO_PUBLIC_URL`
  is set.
- `--dry-run` prints each record's field changes and writes nothing. It does not need `SF_WRITEBACK`.

### 5.2 Invoices (attachments)

**Requirement (Finance/CS, 2026-10-14):** every invoice PDF issued in Odoo must be
attached to its opportunity in SF, in the opportunity's Attachments section. Other
SF workflows depend on this, and it was one of the harder parts of the Celigo
setup.

| Odoo field (`account.move`) | Purpose |
|---|---|
| `invoice_pdf_report_file` / rendered invoice report | PDF attached to the opportunity |
| `invoice_line_ids.sale_line_ids.order_id.sf_opportunity_id` | Which opportunity to attach to (an invoice can span orders) |
| `name`, `invoice_date`, `amount_total`, `payment_state` | Optional invoice summary in SF |

**Confirmed 2026-10-14:** Celigo attaches invoices as classic `Attachment` records
(not Files) on the opportunity, named `<invoice number>.pdf` (e.g. `INVG011414.pdf`),
created by the integration user. The connector does the same, replacing characters
not allowed in filenames (e.g. `/`).

**Open:** whether SF workflows depend on the invoice-number format.

The "NetSuite Financials" box on the opportunity (`celigo_sfnsio__NetSuite_Financial__c`,
a list of invoices and payment status) is reportedly not used by CS. Not
replicated for now.

---

## 6. New fields summary (`sf_connector` Odoo module)

Installed in **v19.0.1.5.0** (`odoo_addons/sf_connector`):

- `res.partner`: `sf_account_id`, `sf_legal_entity_name`, `sf_billing_distribution_method`, `sf_default_billing_entity_id`, `sf_parent_account_id` (readonly; SF ParentId, not `parent_id`, see §1), `sf_vat_rejected` (readonly; SF VAT that Odoo rejected, see §1 VAT handling)
- `sale.order`: `sf_opportunity_id`, `sf_revenue_allocation_company_id`, `sf_award_won_date`, `sf_term_start_date`, `sf_opportunity_type`, `sf_description`, `sf_invoice_notes`, `sf_invoice_additional_info`, `sf_billing_distribution_method`, `sf_vat_number`, `sf_stage`
- `sale.order.line`: `sf_line_id`, `sf_data_vendor`, `sf_coverage_period`, `sf_study_type`, `sf_study_name`, `sf_wave_period`, `sf_delivery`, `sf_information_only`, `sf_is_upsell`, `sf_on_hold`, `sf_removed_in_sf`, `sf_term_start_date`, `sf_term_end_date`, `sf_billing_schedule`

Also installed:

- Data: the 12 Salesforce payment terms (`data/payment_terms.xml`, `noupdate`).
- Dependency: `lb_item_deliverables`. `sale.order.line` writes of `sf_on_hold` set `dispatch_status` on its deliverables, and new deliverables on a held line start on hold (`models/lb_item_deliverable.py`).
- Views: a "Salesforce" tab on the customer and sales order forms, and the `sf_*` line fields as optional order-line columns. All `sf_*` fields are readonly.

Dropped from the plan: `sf_credit_note_id` (no automatic un-billing, §4.4).

Fields use an `sf_` prefix (Odoo reserves `x_` for fields created through the UI). They go in a real addon (version-controlled, in this repo) rather than being
created as manual fields in Settings, so they deploy identically to sandbox and
production.

---

## 7. Remaining open items

| # | Item | Who | Needed for testing? |
|---|---|---|---|
| 1 | Admin: the integration user needs Edit on `Odoo_ID__c`, `Odoo_Record__c`, `Odoo_Sync_Error__c` and `Odoo_Last_Sync__c` (Account, Opportunity) and on `Odoo_Line_ID__c`, `Odoo_Qty_Delivered__c`, `Odoo_Qty_Invoiced__c`, `Odoo_Fulfilled__c` (OpportunityLineItem), and must be able to see the records. **Checked 2026-10-29:** the user can see no Account, Opportunity or line records, Account and Opportunity are not updateable, and the Odoo fields are read-only. Write-back stays stopped for each object until fixed (§5.1). | SF admin | Yes, for write-back |
| 2 | Which attachment mechanism (Attachment vs Files) and file naming the SF invoice workflows rely on. **Answered 2026-10-14:** classic Attachment records named `<invoice number>.pdf`, created by the integration user (§5). Open: whether SF workflows depend on the invoice-number format. | Jason (query below) / CS | Yes, for invoice sync (naming question for CS) |
| 3 | Do Odoo products carry the SF `ProductCode` in `default_code`? | Jason | Yes |
| 4 | Michael Martin's Celigo country alias list | Jason / Michael | Helpful |
| 5 | What "Net 45 PSA" means | Finance | No |
| 6 | Hide information-only lines on the invoice PDF | Implementation | No |
| 7 | Billing-schedule invoice prompts | Implementation | No |
| 8 | Go-live: confirm loading open (stage 07) orders only | Finance | No |
| 9 | Shared customers: confirm or lock to entity | Finance | No |
| 10 | Optional SF confirmation prompt on stage change after sync | SF admin | No |

Diagnostic queries (run with `--target-org mtab-sandbox`):

```
# Open orders whose opportunity has no billing country
sf data query --query "SELECT Id, Name, Account.Name, Billing_Country__c FROM Opportunity WHERE StageName = '07: Award Won / Contract signed' AND Billing_Country__c = null"

# Opportunity billing-country spellings in use
sf data query --query "SELECT Billing_Country__c, COUNT(Id) FROM Opportunity WHERE StageName IN ('07: Award Won / Contract signed', 'Closed') GROUP BY Billing_Country__c ORDER BY COUNT(Id) DESC"

# How invoice PDFs are attached today: classic Attachments...
sf data query --query "SELECT Name, ContentType, CreatedBy.Name, CreatedDate FROM Attachment WHERE ParentId IN (SELECT Id FROM Opportunity WHERE celigo_sfnsio__NetSuite_Id__c != null) ORDER BY CreatedDate DESC LIMIT 10"
# ...or Files (replace the Id with an opportunity that has an invoice)
sf data query --query "SELECT ContentDocument.Title, ContentDocument.FileExtension, ContentDocument.CreatedBy.Name FROM ContentDocumentLink WHERE LinkedEntityId = '006XXXXXXXXXXXXXXX'"
```

---

## 8. Decision log

| Date | Decision | Source |
|---|---|---|
| 2026-10-13 | Keep the `x_` fields on `sale.order.line`; connector doesn't write them | Jason |
| 2026-10-14 | `mTab LLC` = US subsidiary, `G.A.M.M.A` = UK subsidiary; companies created during implementation | Finance |
| 2026-10-14 | Shared customers for testing | Finance |
| 2026-10-14 | Quotation → Confirm is the approval step | Finance |
| 2026-10-14 | Payment terms synced by name, with Net X / Net X EOM definitions | Finance |
| 2026-10-14 | Price books not synced | Finance |
| 2026-10-14 | Info-only lines: synced at price 0, invoiced at zero, hidden on PDF (later) | Finance, CS |
| 2026-10-14 | Billing schedules stored only for testing | Finance |
| 2026-10-14 | VAT kept on the order, as in NetSuite | Finance |
| 2026-10-14 | Invoice notes: internal; Additional Info → invoice Notes | Finance, CS |
| 2026-10-14 | Invoice PDFs attached to the SF opportunity | Finance, CS |
| 2026-10-14 | Removed lines kept at qty 0; fulfilled removals fail with an error; no automatic un-billing | Finance |
| 2026-10-14 | Fax dropped | Finance |
| 2026-10-14 | `Closed` = fully delivered; only stage 07 creates orders | Finance |
| 2026-10-14 | Stage reversals handled in Odoo (flag + Finance activity), not by locking SF | Finance |
| 2026-10-14 | On Hold: line stays listed and flagged; must reach DS immediately | CS |
| 2026-10-15 | ParentId stored in `sf_parent_account_id`, not `parent_id`: Odoo's `parent_id` copies parent addresses into children and pushes changes down | Jason |
| 2026-10-15 | VAT cleaned before write (label removed, spaces/dots/hyphens removed, upper case, ISO country prefix). A rejected VAT is saved without VAT, the SF value is kept in `sf_vat_rejected`, and it is not retried until the SF value changes | Jason |
| 2026-10-27 | Connector-managed sale orders are set to the `DS deliverables` project so CS can see their deliverables in the Delivery Tracker. Set on create and backfilled once where empty; never overwritten. Service products created by the connector have `service_tracking = 'no'`, so the project does not create tasks | Jason |
| 2026-10-27 | Stage reversals: one note per actual reversal (a return to Award Won is marked in the chatter); reversed orders with no stage change are silent; line changes are posted as one note per run listing Finance/CS-facing changes | Jason |
| 2026-10-14 | Deliverable holds are managed only from SF; clearing On Hold releases all of the line's undelivered deliverables | Jason |
