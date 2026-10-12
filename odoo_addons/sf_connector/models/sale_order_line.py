# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    sf_line_id = fields.Char(
        string='Salesforce Line ID',
        index=True,
        copy=False,
        help='Salesforce OpportunityLineItem Id. Upsert key per line.',
    )
    sf_data_vendor = fields.Char(
        string='Data Vendor',
    )
    sf_coverage_period = fields.Char(
        string='Coverage Period',
    )
    sf_study_type = fields.Char(
        string='Study Type',
    )
    sf_study_name = fields.Char(
        string='Study Name',
    )
    sf_wave_period = fields.Char(
        string='Wave Period',
    )
    sf_delivery = fields.Selection(
        selection=[
            ('classic', 'Classic'),
            ('halo', 'Halo'),
            ('both', 'Both'),
        ],
        string='Delivery',
    )
    sf_information_only = fields.Boolean(
        string='Information Only',
    )
    sf_is_upsell = fields.Boolean(
        string='Upsell',
    )
    sf_on_hold = fields.Boolean(
        string='On Hold',
    )
    sf_term_start_date = fields.Date(
        string='Term Start Date',
    )
    sf_term_end_date = fields.Date(
        string='Term End Date',
    )
    # Stored as-is. Odoo does not generate invoices from this value.
    sf_billing_schedule = fields.Char(
        string='Billing Schedule',
    )

    _sf_line_id_unique = models.Constraint(
        'UNIQUE(sf_line_id)',
        'Another order line already has this Salesforce Line ID.',
    )
