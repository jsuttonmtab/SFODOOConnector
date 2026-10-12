# SFODOOConnector

Connector between Salesforce and Odoo 19 for mTab. It replaces the Celigo
Salesforce ↔ NetSuite integration.

## Scope

**Salesforce → Odoo**
- Accounts → customers (`res.partner`), only for accounts with an award-won opportunity
- Award-won Opportunities → sales orders (`sale.order`, created as draft for finance approval)
- Opportunity line items → order lines (`sale.order.line`), feeding the `lb_item_deliverables` module

**Odoo → Salesforce**
- Odoo IDs and links written back to Account / Opportunity
- Invoices (number, amounts, status, PDF)

## Layout

```
docs/
  field-mapping.md      SF ↔ Odoo field map, lock rules, open decisions
tools/
  export_odoo_fields.py Dumps Odoo field definitions to JSON (read-only)
```

Planned:

```
odoo_addons/sf_connector/   Odoo module adding the sf_* fields
connector/                  Python sync service
```

## Environments

| | Odoo | Database |
|---|---|---|
| Sandbox | mpl-odoo (10.10.100.25) | OdooSandbox19 |
| Production | TBD | TBD |
