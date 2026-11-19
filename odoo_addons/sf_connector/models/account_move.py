# -*- coding: utf-8 -*-
import base64
import hashlib
from datetime import timedelta

from odoo import fields, models
from odoo.exceptions import UserError

# Customer invoices and credit notes: the documents the connector attaches to Salesforce (docs/field-mapping.md §5.2).
SF_PDF_TYPES = ('out_invoice', 'out_refund')
# Odoo's standard invoice layouts, which the Send wizard offers and Print can use. Customer documents never get them.
STANDARD_INVOICE_TEMPLATES = ('account.report_invoice_with_payments', 'account.report_invoice')


def sf_without_standard_reports(reports, moves):
    """The report choices for these moves, without Odoo's standard invoice layouts when they are all customer documents
    (out_invoice or out_refund). Mixed or other documents keep the choices Odoo gives them."""
    if moves and all(move.move_type in SF_PDF_TYPES for move in moves):
        return reports.filtered(lambda report: report.report_name not in STANDARD_INVOICE_TEMPLATES)
    return reports

# Company fields that change the printed invoice. A change to any of them makes the connector re-render the invoice
# (sf_render_keys). Keep this list in step with report/report_invoice.xml.
SF_COMPANY_RENDER_FIELDS = (
    'name', 'street', 'street2', 'city', 'zip', 'state_id', 'country_id', 'website',
    'sf_invoice_layout', 'sf_trading_name', 'sf_tax_id', 'sf_header_html', 'sf_notes_html',
    'sf_payment_html', 'sf_legal_html', 'sf_footer_html',
    'sf_epd_days_1', 'sf_epd_percent_1', 'sf_epd_days_2', 'sf_epd_percent_2',
)


def _key_value(value):
    """A field value as text for the render key: a record gives its id, nothing gives an empty string."""
    if isinstance(value, models.BaseModel):
        return str(value.id) if value else ''
    return '' if value is None or value is False else str(value)


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
    sf_pdf_render_key = fields.Char(
        string='Salesforce PDF Render Key',
        copy=False,
        help='The render key (sf_render_keys) of the PDF last sent. Set by the connector. A different key means the '
             'template or the company invoice settings changed, so the connector renders the invoice again.',
    )

    def _sf_invoice_report_xmlid(self):
        """The report action for this invoice's company: mTab LLC (US Letter) or G.A.M.M.A (A4). Both actions use the
        one template in report/report_invoice.xml; the paper size is each action's own. Rendering goes by this xmlid,
        because the two actions share a template name, and a render by template name can pick the wrong paper."""
        self.ensure_one()
        return ('sf_connector.report_sf_invoice_gamma' if self.company_id.sf_invoice_layout == 'gamma'
                else 'sf_connector.report_sf_invoice_llc')

    def _get_available_action_reports(self, is_invoice_report=True):
        return sf_without_standard_reports(
            super()._get_available_action_reports(is_invoice_report=is_invoice_report), self)

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

    def sf_document_title(self):
        """The title printed before the number: "Invoice" or "Credit Note", with "Draft " in front for a draft, so a
        draft cannot pass as a real invoice."""
        self.ensure_one()
        title = 'Credit Note' if self.move_type == 'out_refund' else 'Invoice'
        return f"Draft {title}" if self.state == 'draft' else title

    def action_post(self):
        """Before posting, copy the Salesforce description from the sale line to each invoice line whose copy is empty.
        A draft made before the description reached the sale line then prints it."""
        self._sf_fill_description_copies()
        return super().action_post()

    def _sf_fill_description_copies(self):
        for move in self:
            for line in move.invoice_line_ids:
                if line.display_type != 'product' or line.sf_combined_description:
                    continue
                sale = line.sale_line_ids[:1]
                if sale and sale.sf_combined_description:
                    line.sf_combined_description = sale.sf_combined_description

    def sf_early_payment_offers(self):
        """The early payment offers printed under the invoice total: [{date, percent, amount}]. Display only:
        no accounting entries are made. Days and percentages come from the company (Settings → Companies).
        Credit notes have none."""
        self.ensure_one()
        if self.move_type != 'out_invoice':
            return []
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

    def sf_render_keys(self):
        """{invoice id: render key}. The key covers the sf_connector version that is installed, the report that applies
        to the invoice (mTab LLC or G.A.M.M.A), every company field that is printed (SF_COMPANY_RENDER_FIELDS) and a hash
        of the company logo. The connector re-renders an invoice whose key differs from the key recorded with the last
        PDF sent, so a template change or a change to the company's invoice settings reaches invoices already sent."""
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'sf_connector')], limit=1)
        version = module.installed_version or ''
        keys = {}
        for move in self:
            company = move.company_id
            parts = [version, move._sf_invoice_report_xmlid()]
            parts += [f"{name}={_key_value(company[name])}" for name in SF_COMPANY_RENDER_FIELDS]
            parts.append("logo=" + hashlib.sha256(company.logo or b'').hexdigest())
            keys[move.id] = hashlib.sha256("\n".join(parts).encode('utf-8')).hexdigest()
        return keys

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
