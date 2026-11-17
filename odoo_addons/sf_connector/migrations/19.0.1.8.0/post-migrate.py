# -*- coding: utf-8 -*-
"""Combined Product Description on its own field (Finance and Salesforce admin, 2026-11-17).

Before this version the line name held the Salesforce description (e.g. "Product (2025 W4 USA NVES)" or
"2025 W4 USA NVES"), so the product name could appear twice on the invoice. Now:
- sf_combined_description holds the Salesforce description;
- the line name is the product name only.

For each order line with a product: if the name differs from the product name, the current name is moved to
sf_combined_description and the name is set to the product name. A line already in that state is not touched, so the
script can run again safely. Lines without a product are left as they are.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env['sale.order.line'].search([('product_id', '!=', False)])
    moved = 0
    for line in lines:
        product_name = line.product_id.name
        if not product_name or line.name == product_name:
            continue
        vals = {'name': product_name}
        if not line.sf_combined_description:
            vals['sf_combined_description'] = line.name
        line.write(vals)
        moved += 1
    # the invoice lines keep their own copy of the name; only sale order lines are changed here
    return moved
