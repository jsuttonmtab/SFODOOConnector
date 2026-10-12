# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    sf_opportunity_id = fields.Char(
        string='Salesforce Opportunity ID',
        index=True,
        copy=False,
        help='Salesforce Opportunity Id. Upsert key for the connector.',
    )
    sf_revenue_allocation_company_id = fields.Many2one(
        'res.partner',
        string='Revenue Allocation Company',
    )
    sf_award_won_date = fields.Date(
        string='Award Won Date',
        help='Salesforce Date_Award_Won0__c. Also used as the order date.',
    )
    sf_term_start_date = fields.Date(
        string='Term Start Date',
    )
    sf_opportunity_type = fields.Selection(
        selection=[
            ('new', 'New'),
            ('up_sell', 'Up Sell'),
            ('cross_sell', 'Cross Sell'),
            ('renewal', 'Renewal'),
        ],
        string='Opportunity Type',
    )
    sf_description = fields.Text(
        string='Salesforce Description',
        help='Internal. Not printed on documents.',
    )
    sf_invoice_additional_info = fields.Text(
        string='Additional Invoice Information',
    )
    sf_invoice_notes = fields.Text(
        string='Invoice Notes',
        help='Internal.',
    )
    # Picklist values not yet known; stored as the raw Salesforce value.
    sf_billing_distribution_method = fields.Char(
        string='Billing Distribution Method',
    )

    _sf_opportunity_id_unique = models.Constraint(
        'UNIQUE(sf_opportunity_id)',
        'Another sales order already has this Salesforce Opportunity ID.',
    )
