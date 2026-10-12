# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    sf_account_id = fields.Char(
        string='Salesforce Account ID',
        index=True,
        copy=False,
        help='Salesforce Account Id. Upsert key for the connector.',
    )
    sf_legal_entity_name = fields.Char(
        string='Legal Entity Name',
    )
    # Picklist values not yet known; stored as the raw Salesforce value.
    sf_billing_distribution_method = fields.Char(
        string='Billing Distribution Method',
    )
    sf_default_billing_entity_id = fields.Many2one(
        'res.company',
        string='Default Billing Entity',
        help='Billing entity from Salesforce mTab_Billing_Entity__c. '
             'Sets the default company on new orders for this customer.',
    )

    _sf_account_id_unique = models.Constraint(
        'UNIQUE(sf_account_id)',
        'Another customer already has this Salesforce Account ID.',
    )
