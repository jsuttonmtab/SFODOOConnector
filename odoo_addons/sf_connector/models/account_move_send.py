# -*- coding: utf-8 -*-
"""Which PDF a customer invoice or credit note gets when it is printed, sent, or saved after sending (Print, the Send
wizard, invoice_pdf_report_id): always the company's own report (mTab LLC: US Letter; G.A.M.M.A: A4).

A partner's invoice_template_pdf_report_id is ignored for these documents, even when the Send wizard saved it. The
standard Odoo layouts are never used for them (see ir_actions_report.py and the wizard's choices in account_move.py).
Other documents (bills, payments) keep Odoo's own default.
"""
from odoo import api, models

SF_PDF_TYPES = ('out_invoice', 'out_refund')


class AccountMoveSend(models.AbstractModel):
    _inherit = 'account.move.send'

    @api.model
    def _get_default_pdf_report_id(self, move):
        if move.move_type in SF_PDF_TYPES and move.company_id:
            return self.env.ref(move._sf_invoice_report_xmlid())
        return super()._get_default_pdf_report_id(move)
