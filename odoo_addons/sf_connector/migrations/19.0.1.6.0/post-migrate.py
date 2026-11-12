# -*- coding: utf-8 -*-
"""Net 45 PSA: due 45 days after the end of the invoice month (Finance, 2026-10-28).

data/payment_terms.xml is noupdate, so an upgrade does not change a term that already exists.
This brings the existing "Net 45 PSA" to the new definition in place: the same record, its line
replaced (cleared, then recreated), so no second term is created. A fresh install gets the new
definition from the XML.
"""
from odoo import SUPERUSER_ID, api

NAME = 'Net 45 PSA'
XMLID = 'sf_connector.sf_payment_term_net_45_psa'
LINE = {'value': 'percent', 'value_amount': 100.0, 'nb_days': 45, 'delay_type': 'days_after_end_of_month'}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    term = env.ref(XMLID, raise_if_not_found=False)
    if not term:
        term = env['account.payment.term'].search([('name', '=', NAME)], limit=1)
    if not term:
        return  # not in this database: the XML creates it at install
    lines = term.line_ids
    if len(lines) == 1 and all(lines[field] == value for field, value in LINE.items()):
        return  # already the new definition
    term.write({'line_ids': [(5, 0, 0), (0, 0, LINE)]})
