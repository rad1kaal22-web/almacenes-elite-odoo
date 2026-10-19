# -*- coding: utf-8 -*-
"""The few differences between Odoo versions, in one place: the same code runs on 17.0, 18.0 and 19.0."""


def po_line_uom(line):
    """Unit of measure of a purchase order line («product_uom» up to 18.0, «product_uom_id» from 19.0)."""
    return line.product_uom_id if 'product_uom_id' in line._fields else line.product_uom


def po_line_taxes_field(env):
    """Name of the taxes field of purchase.order.line («taxes_id» up to 18.0, «tax_ids» from 19.0)."""
    return 'tax_ids' if 'tax_ids' in env['purchase.order.line']._fields else 'taxes_id'


def storable_vals(template_model, storable):
    """Values for a new product tracked in stock: «is_storable» from 18.0, type «product» in 17.0 (with Inventory)."""
    if 'is_storable' in template_model._fields:
        return {'is_storable': bool(storable)}
    if storable and 'product' in template_model._fields['type'].get_values(template_model.env):
        return {'type': 'product'}
    return {}


def ea_note(record, body):
    """Chatter note of the module: as the person who did it when they have an email address, otherwise as OdooBot (Odoo cannot
    post as a user without email, and that must never stop an order)."""
    author = record.env.user.partner_id
    if not author.email:
        author = record.env.ref('base.partner_root', raise_if_not_found=False) or author
    return record.message_post(body=body, author_id=author.id)
