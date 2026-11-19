# -*- coding: utf-8 -*-
"""Which PDF an invoice gets when it is printed, sent, or saved after sending (Print, the Send wizard, invoice_pdf_report_id).

Odoo's default is the partner's template, then the journal's, then the standard "Invoice PDF". For a customer invoice or
credit note this module puts the company's own layout (mTab LLC: US Letter; G.A.M.M.A: A4) in place of the standard one:
a partner's explicit choice of template still wins. The journal's template is not used for these documents.
"""
from odoo import api, models

SF_PDF_TYPES = ('out_invoice', 'out_refund')


class AccountMoveSend(models.AbstractModel):
    _inherit = 'account.move.send'

    @api.model
    def _get_default_pdf_report_id(self, move):
        partner_choice = move.commercial_partner_id.with_company(move.company_id).invoice_template_pdf_report_id
        if partner_choice:
            return partner_choice
        if move.move_type in SF_PDF_TYPES and move.company_id:
            return self.env.ref(move._sf_invoice_report_xmlid())
        return super()._get_default_pdf_report_id(move)
