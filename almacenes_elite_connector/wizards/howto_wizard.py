# -*- coding: utf-8 -*-
from odoo import models


class AlmacenesEliteHowto(models.TransientModel):
    """«How it works»: what buying from Almacenes Elite from Odoo looks like, before signing up (nothing is sent)."""
    _name = 'almacenes.elite.howto'
    _description = 'How buying from Almacenes Elite works'

    def action_open_shop(self):
        return self.env['res.config.settings'].create({}).action_ea_open_shop()

    def action_signup(self):
        return self.env['ir.actions.act_window']._for_xml_id('almacenes_elite_connector.action_almacenes_elite_signup')
