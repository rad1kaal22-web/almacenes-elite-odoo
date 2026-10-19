# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

CUSTOMER_TYPES = [('empresa', "Company"), ('autonomo', "Self-employed"), ('colegio', "School"), ('particular', "Individual")]


class AlmacenesEliteSignup(models.TransientModel):
    """Becoming an Almacenes Elite customer from Odoo: the company data is already filled in from this Odoo; the person only
    chooses the type of customer (and the number of employees for a company) and accepts the conditions. Almacenes Elite
    creates the account, applies its prices and sends back the connection key, which is saved here: ready to buy.
    Already a customer (same tax ID): nothing new is created; Almacenes Elite emails an address it already has for that customer
    to confirm it is them, and this Odoo connects to the existing account (same prices and payment terms) when it is confirmed."""
    _name = 'almacenes.elite.signup'
    _description = 'Become an Almacenes Elite customer'

    customer_type = fields.Selection(CUSTOMER_TYPES, string="Type of customer", required=True)
    employees = fields.Integer(string="Number of employees")
    company_name = fields.Char(string="Company or school")
    vat = fields.Char(string="Tax ID")
    contact_name = fields.Char(string="Your name", required=True)
    email = fields.Char(string="Email", required=True)
    phone = fields.Char(string="Phone")
    street = fields.Char(string="Street", required=True)
    zip = fields.Char(string="Postcode", required=True)
    city = fields.Char(string="City", required=True)
    state_id = fields.Many2one('res.country.state', string="Province")
    country_id = fields.Many2one('res.country', string="Country", required=True)
    accept = fields.Boolean(string="I accept the conditions")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        company = self.env.company
        user = self.env.user
        vat = (company.vat or '').replace(' ', '')
        if vat.upper().startswith('ES'):
            vat = vat[2:]
        res.update({
            'company_name': company.name, 'vat': vat, 'contact_name': user.name,
            'email': user.email or company.email, 'phone': user.partner_id.phone or company.phone,
            'street': company.street, 'zip': company.zip, 'city': company.city, 'state_id': company.state_id.id,
            'country_id': company.country_id.id or self.env.ref('base.es').id,
        })
        return {k: v for k, v in res.items() if k in fields_list}

    def action_signup(self):
        self.ensure_one()
        if not self.accept:
            raise UserError(_("Accept the conditions of Almacenes Elite to create the account."))
        if self.customer_type == 'empresa' and self.employees < 1:
            raise UserError(_("Enter the number of employees of the company."))
        if self.customer_type != 'particular' and not (self.company_name and self.vat):
            raise UserError(_("Enter the name and the tax ID of the company or school."))
        company = self.env.company.sudo()
        data = {
            'tipo': self.customer_type, 'empleados': self.employees if self.customer_type == 'empresa' else 0,
            'empresa': self.company_name or '', 'nif': self.vat or '', 'contacto': self.contact_name, 'email': self.email,
            'telefono': self.phone or '', 'calle': self.street, 'cp': self.zip, 'ciudad': self.city,
            'provincia': self.state_id.name or '', 'pais': self.country_id.code or 'ES', 'acepta': True,
            # the address of this Odoo: the confirmation page offers «back to my Odoo» (existing customers)
            'odoo': self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '',
        }
        status, body = self.env['almacenes.elite.api']._ea_signup(data, company=company)
        body = body or {}
        if status == 200 and body.get('ok') and body.get('estado') in ('confirmar', 'revision'):
            # already a customer: connects to the existing account once it is confirmed by email (or checked by Almacenes Elite)
            company._ea_wait_for_confirmation(body)
            return {'type': 'ir.actions.client', 'tag': 'display_notification',
                    'params': {'type': 'info', 'sticky': True,
                               'title': _("You are already a customer: confirm it is you") if body['estado'] == 'confirmar'
                               else _("You are already a customer: Almacenes Elite is checking it"),
                               'message': body.get('mensaje') or '', 'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}
        if body.get('estado') == 'ya_cliente':
            return {'type': 'ir.actions.client', 'tag': 'display_notification',
                    'params': {'type': 'info', 'sticky': True, 'title': _("You are already an Almacenes Elite customer"),
                               'message': self.env['almacenes.elite.api']._ea_error_text(body)}}
        if status != 200 or not body.get('ok') or not body.get('clave'):
            raise UserError(_("Almacenes Elite could not create the account:\n%s", self.env['purchase.order']._ea_problems_text(body)))
        company.write({'ea_consent': True, 'ea_api_key': body['clave']})
        company._ea_connect()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'sticky': True, 'title': _("Welcome to Almacenes Elite"),
                           'message': body.get('mensaje') or _("Your account is ready: you can buy from Purchase › Almacenes Elite."),
                           'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}
