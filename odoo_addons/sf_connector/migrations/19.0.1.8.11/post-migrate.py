# -*- coding: utf-8 -*-
"""Item Description mirrors the Salesforce description (Finance review, 2026-11-19).

From this version the connector writes the Salesforce Combined_Product_Description__c to sf_combined_description and,
in the same call, to item_description (the line's Item Description, read by the Delivery Tracker through the
deliverables). Before this version item_description held the study notes.

This backfill copies sf_combined_description to item_description on every synced line (sf_line_id set), including
delivered and invoiced lines. It is display-only and copies Odoo's own value, so no lock applies. Lines without a
Salesforce description get an empty item_description. Lines already equal are not touched, so the script can run
again safely. Study notes move to sf_study_notes on the next sync, which fills it.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env['sale.order.line'].search([('sf_line_id', '!=', False)])
    changed = 0
    for line in lines:
        wanted = line.sf_combined_description or False
        if (line.item_description or False) == wanted:
            continue
        line.write({'item_description': wanted})
        changed += 1
    return changed
