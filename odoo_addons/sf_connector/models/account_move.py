# -*- coding: utf-8 -*-
import base64
import hashlib

from odoo import fields, models
from odoo.exceptions import UserError

# Customer invoices and credit notes: the documents the connector attaches to Salesforce (docs/field-mapping.md §5.2).
SF_PDF_TYPES = ('out_invoice', 'out_refund')
# The standard customer invoice report (the one printed and sent to customers).
SF_PDF_REPORT = 'account.account_invoices'


class AccountMove(models.Model):
    _inherit = 'account.move'

    sf_pdf_checksum = fields.Char(
        string='Salesforce PDF Checksum',
        copy=False,
        help='SHA-256 of the invoice PDF last attached in Salesforce. Set by the connector after a successful upload.',
    )
    sf_pdf_sent_at = fields.Datetime(
        string='Salesforce PDF Sent At',
        copy=False,
        help='When the connector last recorded a Salesforce attachment for this invoice (UTC). Set by the connector.',
    )

    def sf_invoice_pdf(self):
        """Render the standard customer invoice report for posted customer invoices and credit notes. Called
        by the connector through JSON-2. Returns one dict per invoice: id, name, filename ("/" replaced by "-",
        with ".pdf"), pdf_base64 and checksum (SHA-256 of the PDF). Record rules apply: only invoices the caller
        can read. Anything else (draft, cancelled, bills) is refused."""
        self.check_access('read')
        result = []
        for move in self:
            if move.move_type not in SF_PDF_TYPES or move.state != 'posted':
                raise UserError(
                    f"Only posted customer invoices and credit notes have a Salesforce PDF ({move.name or move.id}).")
            pdf, _unused_type = self.env['ir.actions.report']._render_qweb_pdf(SF_PDF_REPORT, [move.id])
            name = move.name or f'INV{move.id}'
            result.append({
                'id': move.id,
                'name': name,
                'filename': f"{name.replace('/', '-')}.pdf",
                'pdf_base64': base64.b64encode(pdf).decode('ascii'),
                'checksum': hashlib.sha256(pdf).hexdigest(),
            })
        return result
