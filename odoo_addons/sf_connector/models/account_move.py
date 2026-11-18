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

    def _sf_invoice_report_xmlid(self):
        """The report action for this invoice's company: mTab LLC (US Letter) or G.A.M.M.A (A4). Both actions use the
        one template in report/report_invoice.xml; the paper size is each action's own. Rendering goes by this xmlid,
        because the two actions share a template name, and a render by template name can pick the wrong paper."""
        self.ensure_one()
        return ('sf_connector.report_sf_invoice_gamma' if self.company_id.sf_invoice_layout == 'gamma'
                else 'sf_connector.report_sf_invoice_llc')

    def _sf_invoice_report(self):
        return self.env.ref(self._sf_invoice_report_xmlid())

    def action_invoice_print(self):
        """Print: the company's invoice layout. The standard side effects of printing are kept."""
        super().action_invoice_print()
        return self[:1]._sf_invoice_report().report_action(self)

    def sf_format_date(self, value):
        """A date as the invoice prints it, e.g. "1 May 2026" (day without a leading zero)."""
        return f"{value.day} {value:%B %Y}" if value else ''

    def sf_money(self, value):
        """An amount as the reference invoices print it: "$125.99", "£2,110.00", "-$5.00". The currency symbol is
        placed as the currency says; no space is added. Decimals come from the currency."""
        currency = self.currency_id
        digits = currency.decimal_places or 2
        symbol = currency.symbol or currency.name or ''
        amount = value or 0
        text = f"{abs(amount):,.{digits}f}"
        sign = '-' if amount < 0 else ''
        if currency.position == 'after':
            return f"{sign}{text} {symbol}"
        return f"{sign}{symbol}{text}"

    def sf_po_number(self):
        """The PO # printed on the invoice. The invoice reference (ref) is used when it is a customer reference: Odoo
        fills ref with the sale order name for invoices made from an order, and that is not a PO. Otherwise the
        customer's PO on the sale order(s) behind the invoice lines (sale.order.client_order_ref). Otherwise "N/A"."""
        self.ensure_one()
        orders = self.invoice_line_ids.sale_line_ids.order_id
        order_names = set(orders.mapped('name'))
        if self.ref and self.ref not in order_names:
            return self.ref
        refs = [ref for ref in orders.mapped('client_order_ref') if ref]
        return refs[0] if refs else 'N/A'

    def sf_company_website(self):
        """The company website as the invoice prints it: without "http://" or "https://" and without a trailing
        slash, e.g. "www.mtab.com" (NetSuite prints it this way)."""
        self.ensure_one()
        website = (self.company_id.website or '').strip()
        for scheme in ('https://', 'http://'):
            if website.lower().startswith(scheme):
                website = website[len(scheme):]
                break
        return website.rstrip('/')

    def sf_company_city_line(self):
        """The company's place line as the reference prints it: "Yorba Linda, CA 92887 US"."""
        self.ensure_one()
        company = self.company_id
        region = ' '.join(part for part in (
            company.state_id.code if company.state_id else '',
            company.zip or '',
            company.country_id.code if company.country_id else '') if part)
        return ', '.join(part for part in (company.city or '', region) if part)

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
        ".pdf"), pdf_base64 and checksum (SHA-256 of the PDF). Record rules apply: only invoices the caller can read.
        Anything else (draft, cancelled, bills) is refused."""
        self.check_access('read')
        result = []
        for move in self:
            if move.move_type not in SF_PDF_TYPES or move.state != 'posted':
                raise UserError(
                    f"Only posted customer invoices and credit notes have a Salesforce PDF ({move.name or move.id}).")
            pdf, _unused_type = self.env['ir.actions.report']._render_qweb_pdf(
                move._sf_invoice_report_xmlid(), [move.id])
            name = move.name or f'INV{move.id}'
            result.append({
                'id': move.id,
                'name': name,
                'filename': f"{name.replace('/', '-')}.pdf",
                'pdf_base64': base64.b64encode(pdf).decode('ascii'),
                'checksum': hashlib.sha256(pdf).hexdigest(),
            })
        return result
