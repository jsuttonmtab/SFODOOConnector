# -*- coding: utf-8 -*-
"""Invoice lines get the Salesforce description from their sale line (Finance review, 2026-11-19).

An invoice line keeps its own copy of the description, taken when the invoice was created. An invoice made before the
description reached the sale line has an empty copy, so it prints without brackets. This copies the description from
the sale line into each product invoice line whose copy is empty and whose sale line has one. Lines already with a copy
are not touched, so the script can run again safely. Credit notes included. Returns the number of lines changed.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env['account.move.line'].search([
        ('display_type', '=', 'product'),
        ('sf_combined_description', '=', False),
        ('sale_line_ids', '!=', False),
    ])
    changed = 0
    for line in lines:
        sale = line.sale_line_ids[:1]
        if sale and sale.sf_combined_description:
            line.sf_combined_description = sale.sf_combined_description
            changed += 1
    return changed
