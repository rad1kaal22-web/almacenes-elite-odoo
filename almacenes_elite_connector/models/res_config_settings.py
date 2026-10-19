# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .elite_api import DEFAULT_URL


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ea_consent = fields.Boolean(related='company_id.ea_consent', readonly=False)
    ea_api_url = fields.Char(related='company_id.ea_api_url', readonly=False)
    ea_api_key = fields.Char(related='company_id.ea_api_key', readonly=False, groups='base.group_system')
    ea_partner_id = fields.Many2one(related='company_id.ea_partner_id', readonly=True)
    ea_account_name = fields.Char(related='company_id.ea_account_name', readonly=True)
    ea_refresh_prices = fields.Boolean(related='company_id.ea_refresh_prices', readonly=False)
    ea_replenish_remind = fields.Boolean(related='company_id.ea_replenish_remind', readonly=False)
    ea_replenish_user_id = fields.Many2one(related='company_id.ea_replenish_user_id', readonly=False)
    ea_shipping_rule = fields.Selection(related='company_id.ea_shipping_rule', readonly=True)
    ea_shipping_min = fields.Float(related='company_id.ea_shipping_min', readonly=True)
    ea_shipping_amount = fields.Float(related='company_id.ea_shipping_amount', readonly=True)
    ea_payment_mode = fields.Selection(related='company_id.ea_payment_mode', readonly=True)
    ea_payment_request_date = fields.Datetime(related='company_id.ea_payment_request_date', readonly=True)
    ea_web_addresses = fields.Selection(related='company_id.ea_web_addresses', readonly=False)
    ea_import_bills = fields.Boolean(related='company_id.ea_import_bills', readonly=False)
    ea_bills_all = fields.Boolean(related='company_id.ea_bills_all', readonly=False)
    ea_post_bills = fields.Boolean(related='company_id.ea_post_bills', readonly=False)
    ea_product_storable = fields.Boolean(related='company_id.ea_product_storable', readonly=False)
    ea_fetch_images = fields.Boolean(related='company_id.ea_fetch_images', readonly=False)
    ea_last_sync = fields.Datetime(related='company_id.ea_last_sync', readonly=True)
    ea_last_error = fields.Char(related='company_id.ea_last_error', readonly=True)
    ea_signup_sent_to = fields.Char(related='company_id.ea_signup_sent_to', readonly=True)
    ea_signup_date = fields.Datetime(related='company_id.ea_signup_date', readonly=True)
    ea_price_rounding = fields.Boolean(string="Prices rounded to 2 decimals", compute='_compute_ea_price_rounding')

    def _compute_ea_price_rounding(self):
        # Odoo 17 rounds unit prices to the «Product Price» precision (2 by default) and Almacenes Elite prices have 4 decimals;
        # from 18.0 unit prices are not rounded and this is always False.
        digits = self.env['purchase.order.line']._fields['price_unit'].get_digits(self.env)
        low = bool(digits and digits[1] < 4)
        for settings in self:
            settings.ea_price_rounding = low

    def action_ea_open_shop(self):
        """See the shop before signing up: almaceneselite.com as any visitor (with its public prices)."""
        self.ensure_one()
        base = (self.env.company.ea_api_url or DEFAULT_URL).rstrip('/')
        return {'type': 'ir.actions.act_url', 'url': base + '/shop', 'target': 'new'}

    def action_ea_howto(self):
        """«How it works»: the four steps, with what it looks like."""
        return self.env['ir.actions.act_window']._for_xml_id('almacenes_elite_connector.action_almacenes_elite_howto')

    def action_ea_connect(self):
        """Checks the key, reads the customer account and creates (or updates) Almacenes Elite as a vendor."""
        self.ensure_one()
        self.execute()   # save what was typed (key, consent) first
        company = self.env.company
        if not company.ea_consent:
            raise UserError(_("Tick «Enable the connection» first: no data is exchanged with Almacenes Elite until you do."))
        company._ea_connect()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'sticky': False, 'title': _("Connected to Almacenes Elite"),
                       'message': _("Customer account: %s", company.ea_account_name),
                       'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}},
        }

    def action_ea_request_payment_change(self):
        """One click: Almacenes Elite studies another payment method for your account and contacts you."""
        return self.env.company._ea_request_payment_change()

    def action_ea_check_signup(self):
        """Already a customer: ask now instead of waiting for the automatic check (every few minutes)."""
        self.ensure_one()
        company = self.env.company
        result = company._ea_check_signup()
        if result == 'connected' and company.ea_account_name:
            params = {'type': 'success', 'sticky': False, 'title': _("Connected to Almacenes Elite"),
                      'message': _("Customer account: %s", company.ea_account_name)}
        elif result == 'connected':
            params = {'type': 'warning', 'sticky': True, 'title': _("Key saved: finish with «Connect and check»"),
                      'message': company.ea_last_error or ''}
        elif result == 'waiting':
            params = {'type': 'info', 'sticky': False, 'title': _("Not confirmed yet"),
                      'message': (_("Press «Sí, conectar» in the email sent to %s. This Odoo connects by itself as soon as it is confirmed.",
                                    company.ea_signup_sent_to) if company.ea_signup_sent_to
                                  else _("Almacenes Elite is checking your request: they will contact you. This Odoo connects by itself when it is done."))}
        else:
            params = {'type': 'warning', 'sticky': True, 'title': _("The connection was not confirmed"),
                      'message': company.ea_last_error or _("Ask for it again with «Create my account or link the one I have».")}
        params['next'] = {'type': 'ir.actions.client', 'tag': 'soft_reload'}
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': params}

    def action_ea_cancel_signup(self):
        self.ensure_one()
        self.env.company._ea_cancel_signup()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ea_disconnect(self):
        self.ensure_one()
        company = self.env.company.sudo()
        company.write({'ea_api_key': False, 'ea_consent': False})
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ea_sync_now(self):
        self.ensure_one()
        self.env['purchase.order']._ea_sync_company(self.env.company)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_ea_price_digits(self):
        """Opt-in: 4 decimals for product prices, so purchase orders and bills add up exactly like Almacenes Elite invoices."""
        self.ensure_one()
        precision = self.env['decimal.precision'].sudo().search([('name', '=', 'Product Price')], limit=1)
        if precision and precision.digits < 4:
            precision.digits = 4
        return {'type': 'ir.actions.client', 'tag': 'reload'}
