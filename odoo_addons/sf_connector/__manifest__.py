# -*- coding: utf-8 -*-
{
    'name': 'Salesforce Connector Fields',
    'version': '19.0.1.5.0',
    'summary': 'Salesforce ID and mapped fields for customers, orders and order lines',
    'description': """
Adds the sf_* fields used by the Salesforce -> Odoo connector.
See docs/field-mapping.md, section 6.
""",
    'category': 'Sales',
    'author': 'mTab',
    'license': 'LGPL-3',
    'depends': ['sale', 'lb_item_deliverables'],
    'data': [
        'data/payment_terms.xml',
        'views/res_partner_views.xml',
        'views/sale_order_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
