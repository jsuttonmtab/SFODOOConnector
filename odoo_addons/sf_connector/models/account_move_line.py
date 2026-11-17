# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    sf_combined_description = fields.Char(
        string='Combined Product Description',
        copy=False,
        help='Copied from the sale order line when the invoice is created. Printed after the product name.',
    )
    sf_information_only = fields.Boolean(
        string='Information Only',
        copy=False,
        help='Copied from the sale order line. Invoiced at 0 and not printed on the invoice PDF.',
    )
