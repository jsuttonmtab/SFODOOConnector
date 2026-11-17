# -*- coding: utf-8 -*-
import base64
import hashlib
from datetime import timedelta

from odoo import fields, models
from odoo.exceptions import UserError

# Customer invoices and credit notes: the documents the connector attaches to Salesforce (docs/field-mapping.md §5.2).
SF_PDF_TYPES = ('out_invoice', 'out_refund')


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

    def _sf_invoice_report(self):
        """The invoice report for this invoice's company: mTab LLC (US Letter) or G.A.M.M.A (A4). Both use the one
        template in report/report_invoice.xml; the paper size is the report's own."""
        self.ensure_one()
        xmlid = ('sf_connector.report_sf_invoice_gamma' if self.company_id.sf_invoice_layout == 'gamma'
                 else 'sf_connector.report_sf_invoice_llc')
        return self.env.ref(xmlid)

    def action_invoice_print(self):
        """Print: the company's invoice layout. The standard side effects of printing are kept."""
        super().action_invoice_print()
        return self[:1]._sf_invoice_report().report_action(self)

    def sf_early_payment_offers(self):
        """The early payment offers printed under the invoice total: [{date, percent, amount}]. Display only:
        no accounting entries are made. Days and percentages come from the company (Settings → Companies)."""
        self.ensure_one()
        company = self.company_id
        offers = []
        for days, percent in ((company.sf_epd_days_1, company.sf_epd_percent_1),
                              (company.sf_epd_days_2, company.sf_epd_percent_2)):
            if not days or not percent or not self.invoice_date:
                continue
            amount = self.currency_id.round(self.amount_total * (1 - percent / 100.0))
            offers.append({
                'date': self.invoice_date + timedelta(days=days),
                'percent': percent,
                'amount': amount,
            })
        return offers

    def sf_invoice_pdf(self):
        """Render the invoice report (the one Print uses) for posted customer invoices and credit notes. Called by
        the connector through JSON-2. Returns one dict per invoice: id, name, filename ("/" replaced by "-", with
        ".pdf"), pdf_base64 and checksum (SHA-256 of the PDF). Record rules apply: only invoices the caller can
        read. Anything else (draft, cancelled, bills) is refused."""
        self.check_access('read')
        result = []
        for move in self:
            if move.move_type not in SF_PDF_TYPES or move.state != 'posted':
                raise UserError(
                    f"Only posted customer invoices and credit notes have a Salesforce PDF ({move.name or move.id}).")
            report = move._sf_invoice_report()
            pdf, _unused_type = self.env['ir.actions.report']._render_qweb_pdf(report.report_name, [move.id])
            name = move.name or f'INV{move.id}'
            result.append({
                'id': move.id,
                'name': name,
                'filename': f"{name.replace('/', '-')}.pdf",
                'pdf_base64': base64.b64encode(pdf).decode('ascii'),
                'checksum': hashlib.sha256(pdf).hexdigest(),
            })
        return result
