# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    # Which invoice layout the company uses (report/report_invoice.xml). mTab LLC: US Letter. G.A.M.M.A: A4.
    sf_invoice_layout = fields.Selection(
        selection=[('llc', 'mTab LLC'), ('gamma', 'G.A.M.M.A')],
        string='Invoice Layout',
        default='llc',
        required=True,
    )
    # Text and HTML below are per company and are left empty in code: they are entered in Settings → Companies.
    sf_trading_name = fields.Char(
        string='Trading Name (under the title)',
        help='Printed under the invoice title, e.g. "A trading name of GAMMA ASSOCIATES LTD.". Empty: nothing printed.',
    )
    sf_tax_id = fields.Char(string='Tax ID (invoice header)')
    sf_header_html = fields.Html(
        string='Header Block (right of the title)',
        help='Company block on the right of the title, e.g. the Registered Office and VAT number for G.A.M.M.A.',
    )
    sf_notes_html = fields.Html(string='Invoice Notes')
    sf_payment_html = fields.Html(string='Payment and Wire Instructions')
    sf_legal_html = fields.Html(string='Legal Payment Paragraph')
    sf_footer_html = fields.Html(
        string='Footer',
        help='Finance and support emails, website, phone, and the legal name and registration line.',
    )
    # Early payment discount offer, shown on the invoice only (no accounting entries).
    sf_epd_days_1 = fields.Integer(string='Early Payment Offer 1: Days After Invoice Date', default=10)
    sf_epd_percent_1 = fields.Float(string='Early Payment Offer 1: Discount %', default=3.0, digits=(6, 2))
    sf_epd_days_2 = fields.Integer(string='Early Payment Offer 2: Days After Invoice Date', default=21)
    sf_epd_percent_2 = fields.Float(string='Early Payment Offer 2: Discount %', default=2.0, digits=(6, 2))
