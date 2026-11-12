# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    sf_product_id = fields.Char(
        string='Salesforce Product ID',
        copy=False,
        help='Salesforce Product2 Id. The connector matches a line to the product with this Id first. '
             'A product matched by code or name is stamped with the Id, so the next run matches by Id.',
    )

    # A NULL does not count as a duplicate, so the constraint applies only where an Id is set.
    _sf_product_id_unique = models.Constraint(
        'UNIQUE(sf_product_id)',
        'Another product already has this Salesforce Product ID.',
    )
