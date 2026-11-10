"""Apply presentation-only CPF/CNPJ labels via an Odoo shell environment.

Run with `apply(env)` from `odoo shell`; it commits only after render checks.
Does not install fiscal modules or change tax numbers, taxes, or journal entries.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from lxml import html


LABEL = '''<t t-name="odoo_desktop.br_tax_label"><t t-set="desktop_digits" t-value="(desktop_tax_number or '').replace('.', '').replace('/', '').replace('-', '').replace(' ', '').removeprefix('BR')"/><t t-out="'CPF' if desktop_digits.isdigit() and len(desktop_digits) == 11 else ('CNPJ' if desktop_digits.isdigit() and len(desktop_digits) == 14 else 'CPF/CNPJ')"/></t>'''

NUMBER = '''<t t-name="odoo_desktop.br_tax_number"><t t-set="desktop_digits" t-value="(desktop_tax_number or '').replace('.', '').replace('/', '').replace('-', '').replace(' ', '').removeprefix('BR')"/><t t-if="desktop_digits.isdigit() and len(desktop_digits) == 11" t-out="'%s.%s.%s-%s' % (desktop_digits[:3], desktop_digits[3:6], desktop_digits[6:9], desktop_digits[9:])"/><t t-elif="desktop_digits.isdigit() and len(desktop_digits) == 14" t-out="'%s.%s.%s/%s-%s' % (desktop_digits[:2], desktop_digits[2:5], desktop_digits[5:8], desktop_digits[8:12], desktop_digits[12:])"/><t t-else="" t-out="desktop_tax_number or ''"/></t>'''

PARTNER_LINE = '''<t t-name="odoo_desktop.br_partner_tax_line"><t t-set="desktop_ident" t-value="o.partner_id._get_preferred_tax_identifier_vals()"/><t t-if="desktop_ident and o.company_id.account_fiscal_country_id.code == 'BR' and o.partner_id.country_id.code in (False, 'BR')"><t t-call="odoo_desktop.br_tax_label" desktop_tax_number="desktop_ident.get('value')"/>: <t t-call="odoo_desktop.br_tax_number" desktop_tax_number="desktop_ident.get('value')"/></t><t t-else="" t-out="o._get_document_partner_ident_line()"/></t>'''

COMPANY_REPLACEMENT = '''<span><t t-if="company.country_id.code == 'BR'" t-call="odoo_desktop.br_tax_label" desktop_tax_number="forced_vat or company.vat"/><span t-else="" t-field="company.country_id.vat_label">Tax ID</span>: </span>'''

COMPANY = '<data>' + ''.join(
    f'<xpath expr="{expression}" position="replace">{COMPANY_REPLACEMENT}</xpath>'
    for expression in (
        "//li[@t-if='not forced_vat']//span[@t-field='company.country_id.vat_label']",
        "//li[@t-else='']//span[@t-field='company.country_id.vat_label']",
    )
) + ''.join(
    f'''<xpath expr="//span[@t-out='{expression}']" position="replace"><span><t t-if="company.country_id.code == 'BR'" t-call="odoo_desktop.br_tax_number" desktop_tax_number="{expression}"/><t t-else="" t-out="{expression}"/></span></xpath>'''
    for expression in ('company.vat', 'forced_vat')
) + '</data>'

INVOICE = '<data>' + ''.join(
    f'''<xpath expr="//div[@id='{block}']/t[@t-out='o._get_document_partner_ident_line()']" position="replace"><t t-call="odoo_desktop.br_partner_tax_line" o="o"/></xpath>'''
    for block in ('partner_vat_address_not_same_as_shipping', 'partner_vat_address_same_as_shipping', 'partner_vat_no_shipping')
) + '</data>'


def formatted_number(value):
    digits = (value or '').replace('.', '').replace('/', '').replace('-', '').replace(' ', '').removeprefix('BR')
    if digits.isdigit() and len(digits) == 11:
        return f'{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}'
    if digits.isdigit() and len(digits) == 14:
        return f'{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}'
    return value or ''


def render_checks(env):
    View = env['ir.ui.view'].with_context(lang='pt_BR')
    for number, expected in [('52998224725', 'CPF'), ('529.982.247-25', 'CPF'), ('11222333000181', 'CNPJ'), ('11.222.333/0001-81', 'CNPJ'), ('123', 'CPF/CNPJ')]:
        rendered = str(View._render_template('odoo_desktop.br_tax_label', {'desktop_tax_number':number})).strip()
        assert rendered == expected, (expected, rendered)
        rendered_number = str(View._render_template('odoo_desktop.br_tax_number', {'desktop_tax_number':number})).strip()
        assert rendered_number == formatted_number(number), 'Tax number formatting failed.'
        if expected != 'CPF/CNPJ':
            partner = env['res.partner'].new({'name':'Presentation test', 'vat':number, 'country_id':env.ref('base.br').id})
            invoice = env['account.move'].new({'partner_id':partner, 'company_id':env.company.id, 'move_type':'out_invoice'})
            tax_number = partner._get_preferred_tax_identifier_vals()['value']
            rendered = str(View._render_template('odoo_desktop.br_partner_tax_line', {'o':invoice})).strip()
            assert rendered == f'{expected}: {formatted_number(tax_number)}', 'Customer document formatting failed.'
    foreign = env['res.partner'].new({'name':'Foreign presentation test', 'vat':'123456789', 'country_id':env.ref('base.us').id})
    invoice = env['account.move'].new({'partner_id':foreign, 'company_id':env.company.id, 'move_type':'out_invoice'})
    assert str(View._render_template('odoo_desktop.br_partner_tax_line', {'o':invoice})).strip() == invoice._get_document_partner_ident_line()


def apply(env):
    os.umask(0o077)
    country = env.ref('base.br').with_context(lang='pt_BR')
    View = env['ir.ui.view'].with_context(lang=None)
    specifications = [
        ('br_tax_label', LABEL, None),
        ('br_tax_number', NUMBER, None),
        ('br_partner_tax_line', PARTNER_LINE, None),
        ('br_company_tax_label', COMPANY, env.ref('web.company_address_list')),
        ('br_invoice_tax_label', INVOICE, env.ref('account.report_invoice_document')),
    ]
    env.cr.execute('SELECT vat_label FROM res_country WHERE id=%s', (country.id,))
    previous = {'country_id':country.id, 'vat_label':env.cr.fetchone()[0], 'views':[]}
    for name, _, _ in specifications:
        view = View.search([('key', '=', 'odoo_desktop.' + name)], limit=1)
        previous['views'].append({'key':'odoo_desktop.' + name, 'existed':bool(view),
            'id':view.id if view else None, 'arch_db':view.arch_db if view else None,
            'active':view.active if view else None})
    backup = Path.home() / '.local/share/local.odoo.desktop/backups' / ('before-br-labels-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    (backup / 'presentation-state.json').write_text(json.dumps(previous, ensure_ascii=False, indent=2))
    # Update the source label and Portuguese translation through the ORM.
    country.with_context(lang=None).write({'vat_label':'CPF/CNPJ'})
    country.write({'vat_label':'CPF/CNPJ'})
    for name, architecture, parent in specifications:
        key = 'odoo_desktop.' + name
        view = View.search([('key', '=', key)], limit=1)
        values = dict(name=key, key=key, type='qweb', arch_db=architecture, active=True, priority=80)
        if parent:
            values.update(inherit_id=parent.id, mode='extension')
        if view:
            view.write(values)
        else:
            view = View.create(values)
        reference = env['ir.model.data'].search([('module', '=', 'odoo_desktop'), ('name', '=', name)], limit=1)
        if not reference:
            env['ir.model.data'].create(dict(module='odoo_desktop', name=name, model='ir.ui.view', res_id=view.id, noupdate=True))
    render_checks(env)
    # Validate the actual company layout, without modifying the company's ID.
    for company in env['res.company'].search([]).filtered(lambda record: record.country_id.code == 'BR'):
        text = str(View._render_template('web.company_address_list', {'company':company, 'forced_vat':False}))
        digits = ''.join(character for character in (company.vat or '') if character in '0123456789')
        if len(digits) == 11:
            assert re.search(r'\bCPF\s*:', text) and formatted_number(company.vat) in text, 'Company CPF label did not render.'
    invoice = env['account.move'].search([('move_type', '=', 'out_invoice')], limit=1)
    if invoice and invoice.company_id.country_id.code == 'BR':
        document, _ = env['ir.actions.report'].with_context(lang='pt_BR')._render_qweb_html('account.account_invoices', invoice.ids)
        tree = html.fromstring(document)
        blocks = tree.xpath("//ul[@name='company_address_list']")
        digits = re.sub(r'\D', '', invoice.fiscal_position_id.foreign_vat or invoice.company_id.vat or '')
        label = 'CPF' if len(digits) == 11 else 'CNPJ' if len(digits) == 14 else 'CPF/CNPJ'
        assert any(re.search(re.escape(label) + r'\s*:', block.text_content()) for block in blocks), 'Full invoice company tax label failed.'
        assert any(formatted_number(invoice.fiscal_position_id.foreign_vat or invoice.company_id.vat) in block.text_content() for block in blocks), 'Full invoice company document formatting failed.'
    env.cr.commit()
    print('Formulário: CPF/CNPJ. Relatórios: CPF XXX.XXX.XXX-XX; CNPJ XX.XXX.XXX/XXXX-XX.')
    print('Verificações QWeb concluídas; documentos e configuração fiscal não foram alterados.')
    print(f'Configuração anterior preservada em: {backup}')
