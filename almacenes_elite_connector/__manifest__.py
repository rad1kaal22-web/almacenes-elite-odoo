# -*- coding: utf-8 -*-
{
    'name': 'Almacenes Elite Connector',
    'version': '19.0.1.1.0',
    'category': 'Inventory/Purchase',
    'summary': 'Buy office supplies from Almacenes Elite from Odoo: shop on almaceneselite.com signed in, orders back as purchase orders, tracking and vendor bills',
    'description': """
Buy office supplies, stationery and school material from Almacenes Elite (Spain) from your Odoo.

* Shop on almaceneselite.com already signed in (Purchase › Almacenes Elite), with your prices, approvals and payment method.
* The order comes back to Odoo as a confirmed purchase order, with the same total.
* An RFQ of your Odoo (for instance, your replenishment) opens on the website with its products in the cart.
* Your RFQs show what is out of stock at Almacenes Elite, when it is expected, an alternative with stock and how much is
  missing to avoid shipping costs. Prices are updated every night.
* Order status, shipments and tracking numbers come back to the purchase order; returns and claims open on the website.
* Almacenes Elite invoices and credit notes become draft vendor bills, linked to your purchase order and with the original PDF.
* The B2B portal of almaceneselite.com (approvals, cost centers, reports), one click away.

Not a customer yet: create your account from Odoo in one step (your company data is filled in; your prices apply at once).
Already a customer: the same step recognises your tax ID and links Odoo to your account, with your usual prices and payment
terms, after you confirm it by email. Data is only sent after you enable the connection in Purchase settings.
""",
    'author': 'Almacenes Elite S.L.',
    'website': 'https://almaceneselite.com',
    'support': 'pedidos@almaceneselite.com',
    'license': 'LGPL-3',
    'depends': ['purchase'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'wizards/signup_wizard_views.xml',
        'wizards/howto_wizard_views.xml',
        'views/res_config_settings_views.xml',
        'views/purchase_order_views.xml',
        'views/menus.xml',
    ],
    'images': ['static/description/banner.png', 'static/description/main_screenshot.png'],
    'installable': True,
    'application': True,
}
