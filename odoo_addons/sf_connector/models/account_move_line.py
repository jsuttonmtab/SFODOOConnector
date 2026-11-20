# -*- coding: utf-8 -*-
import re

from odoo import fields, models


def _same_text(first, second):
    """True when two texts are the same, ignoring case, extra spaces and surrounding brackets. Empty texts are not the same."""
    def norm(text):
        text = re.sub(r"\s+", " ", text or "").strip().lower()
        while text.startswith("(") and text.endswith(")"):
            text = text[1:-1].strip()
        return text
    return bool(norm(first)) and norm(first) == norm(second)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def sf_item_text(self):
        """(text, description) as the invoice prints them: the text in front, and the Salesforce description in brackets
        after it. The description is printed in brackets only when it adds something. It is printed as plain text (no
        brackets, no italics; the second value is empty) when it is the same as the line text or the product name, or
        when nothing is left of the name once the description is removed.

        Same means: ignoring case, extra spaces and surrounding brackets. Invoices made before 19.0.1.8.0 keep "product +
        description" in the line name: when the name ends with the description, or contains it, that part is removed and
        trimmed. A hand-edited name that does not contain the description prints as written. The product name is never
        put back, and an empty description prints the name only."""
        name = (self.name or '').strip()
        description = (self.sf_combined_description or '').strip()
        if not description:
            return name, ''
        if _same_text(name, description):
            return name, ''
        if name.endswith(description):
            stripped = name[:-len(description)].strip()
        elif description in name:
            stripped = name.replace(description, '', 1).strip()
        else:
            stripped = name
        if not stripped:
            return description, ''
        product = self.product_id.name if self.product_id else ''
        if _same_text(description, product):
            return stripped, ''
        return stripped, description

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
