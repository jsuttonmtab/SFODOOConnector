# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def sf_item_text(self):
        """(name, description) as the invoice prints them. Invoices made before 19.0.1.8.0 keep "product + description"
        in the line name. When the name ends with the Salesforce description, or contains it, that part is removed from the
        name and trimmed; the description is then printed in brackets. Otherwise the name is printed as it is (Finance may
        have edited it by hand). The product name is never put back."""
        name = (self.name or '').strip()
        description = (self.sf_combined_description or '').strip()
        if not description:
            return name, ''
        if name.endswith(description):
            name = name[:-len(description)].strip()
        elif description in name:
            name = name.replace(description, '', 1).strip()
        return name, description

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
