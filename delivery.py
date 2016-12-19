# This file is part of sale_pos module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from decimal import Decimal
from datetime import datetime
from trytond.model import ModelView, fields, ModelSQL, Workflow
from trytond.pool import PoolMeta, Pool
from trytond.transaction import Transaction
from trytond.pyson import Bool, Eval, Or, If, Id
from trytond.wizard import (Wizard, StateView, StateAction, StateTransition,
    Button)
from trytond.modules.company import CompanyReport
from trytond.report import Report

__all__ = ['Delivery', 'DeliveryLine', 'DeliveryLineTax', 'ValidatedInvoice',
'DeliveryNoteReport']
__metaclass__ = PoolMeta

_ZERO = Decimal(0)

conversor = None
try:
    from numword import numword_es
    conversor = numword_es.NumWordES()
except:
    print("Warning: Does not possible import numword module!")
    print("Please install it...!")


class Delivery(ModelSQL, ModelView):
    'Delivery'
    __name__ = 'sale.delivery'

    company = fields.Many2One('company.company', 'Company', required=True,
        states={
            'readonly': (Eval('state') != 'draft') | Eval('lines', [0]),
            },
        domain=[
            ('id', If(Eval('context', {}).contains('company'), '=', '!='),
                Eval('context', {}).get('company', -1)),
            ],
        depends=['state'], select=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('saved', 'Saved'),
        ('anulled', 'Anulled'),
        ('invoiced', 'Invoiced'),
    ], 'State', readonly=True, required=True)

    number = fields.Char('Number', readonly=True, help="Delivery Note Number")

    delivery_date = fields.Date('Date',
        states={
            'readonly': ~Eval('state').in_(['draft', 'quotation']),
            'required': ~Eval('state').in_(['draft', 'quotation', 'cancel']),
            },
        depends=['state'])

    party = fields.Many2One('party.party', 'Party', required=True, select=True,
        states={
            'readonly': ((Eval('state') != 'draft')
                | (Eval('lines', [0]) & Eval('party'))),
            },
        depends=['state'])
    party_lang = fields.Function(fields.Char('Party Language'),
        'on_change_with_party_lang')
    warehouse = fields.Many2One('stock.location', 'Warehouse',
        domain=[('type', '=', 'warehouse')], states={
            'readonly': Eval('state') != 'draft',
            },
        depends=['state'])
    currency = fields.Many2One('currency.currency', 'Currency', required=True,
        states={
            'readonly': (Eval('state') != 'draft') |
                (Eval('lines', [0]) & Eval('currency', 0)),
            },
        depends=['state'])
    currency_digits = fields.Function(fields.Integer('Currency Digits'),
        'on_change_with_currency_digits')
    lines = fields.One2Many('sale.delivery_line', 'delivery', 'Lines', states={
            'readonly': Eval('state') != 'draft',
            },
        depends=['party', 'state'])
    comment = fields.Text('Comment')
    untaxed_amount = fields.Function(fields.Numeric('Untaxed',
            digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits']), 'get_amount')
    untaxed_amount_cache = fields.Numeric('Untaxed Cache',
        digits=(16, Eval('currency_digits', 2)),
        readonly=True,
        depends=['currency_digits'])
    tax_amount = fields.Function(fields.Numeric('Tax',
            digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits']), 'get_amount')
    tax_amount_cache = fields.Numeric('Tax Cache',
        digits=(16, Eval('currency_digits', 2)),
        readonly=True,
        depends=['currency_digits'])
    total_amount = fields.Function(fields.Numeric('Total',
            digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits']), 'get_amount')
    total_amount_cache = fields.Numeric('Total Tax',
        digits=(16, Eval('currency_digits', 2)),
        readonly=True,
        depends=['currency_digits'])

    moves = fields.Function(fields.One2Many('stock.move', None, 'Moves'),
        'get_moves')

    @classmethod
    def __setup__(cls):
        super(Delivery, cls).__setup__()

        cls._states_cached = ['invoiced', 'anulled']

        cls._buttons.update({
                'consolidate': {
                    'invisible': Eval('state').in_(['draft', 'invoiced']),
                    },

                'save': {
                    'invisible': Eval('state').in_(['saved', 'invoiced', 'anulled']),
                    },

                })

    @classmethod
    def default_warehouse(cls):
        Location = Pool().get('stock.location')
        locations = Location.search(cls.warehouse.domain)
        return locations[0].id

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_currency():
        Company = Pool().get('company.company')
        company = Transaction().context.get('company')
        if company:
            return Company(company).currency.id

    @staticmethod
    def default_currency_digits():
        Company = Pool().get('company.company')
        company = Transaction().context.get('company')
        if company:
            return Company(company).currency.digits
        return 2

    @staticmethod
    def default_invoice_state():
        return 'none'

    @fields.depends('currency')
    def on_change_with_currency_digits(self, name=None):
        if self.currency:
            return self.currency.digits
        return 2

    def get_tax_context(self):
        res = {}
        if self.party and self.party.lang:
            res['language'] = self.party.lang.code
        return res

    @fields.depends('party')
    def on_change_with_party_lang(self, name=None):
        Config = Pool().get('ir.configuration')
        if self.party and self.party.lang:
            return self.party.lang.code
        return Config.get_language()

    @fields.depends('lines', 'currency', 'party')
    def on_change_lines(self):
        pool = Pool()
        Tax = pool.get('account.tax')
        Invoice = pool.get('account.invoice')
        Configuration = pool.get('account.configuration')

        config = Configuration(1)

        changes = {
            'untaxed_amount': Decimal('0.0'),
            'tax_amount': Decimal('0.0'),
            'total_amount': Decimal('0.0'),
            }

        if self.lines:
            context = self.get_tax_context()
            taxes = {}

            def round_taxes():
                if self.currency:
                    for key, value in taxes.iteritems():
                        taxes[key] = self.currency.round(value)

            for line in self.lines:
                if getattr(line, 'type', 'line') != 'line':
                    continue
                changes['untaxed_amount'] += (getattr(line, 'amount', None)
                    or Decimal(0))

                with Transaction().set_context(context):
                    tax_list = Tax.compute(getattr(line, 'taxs', []),
                        getattr(line, 'unit_price', None) or Decimal('0.0'),
                        getattr(line, 'quantity', None) or 0.0)
                for tax in tax_list:
                    key, val = Invoice._compute_tax(tax, 'out_invoice')
                    if key not in taxes:
                        taxes[key] = val['amount']
                    else:
                        taxes[key] += val['amount']
                if config.tax_rounding == 'line':
                    round_taxes()
            if config.tax_rounding == 'document':
                round_taxes()
            changes['tax_amount'] = sum(taxes.itervalues(), Decimal('0.0'))
        if self.currency:
            changes['untaxed_amount'] = self.currency.round(
                changes['untaxed_amount'])
            changes['tax_amount'] = self.currency.round(changes['tax_amount'])
        changes['total_amount'] = (changes['untaxed_amount']
            + changes['tax_amount'])
        if self.currency:
            changes['total_amount'] = self.currency.round(
                changes['total_amount'])
        return changes

    def get_amount2words(self, value):
            if conversor:
                return (conversor.cardinal(int(value))).upper()
            else:
                return ''

    def get_tax_amount(self):
        pool = Pool()
        Tax = pool.get('account.tax')
        Invoice = pool.get('account.invoice')
        Configuration = pool.get('account.configuration')

        config = Configuration(1)

        context = self.get_tax_context()
        taxes = {}

        def round_taxes():
            for key, value in taxes.iteritems():
                taxes[key] = self.currency.round(value)

        for line in self.lines:
            if line.type != 'line':
                continue
            with Transaction().set_context(context):
                tax_list = Tax.compute(line.taxs, line.unit_price,
                    line.quantity)
            for tax in tax_list:
                key, val = Invoice._compute_tax(tax, 'out_invoice')
                if key not in taxes:
                    taxes[key] = val['amount']
                else:
                    taxes[key] += val['amount']
            if config.tax_rounding == 'line':
                round_taxes()
        if config.tax_rounding == 'document':
            round_taxes()
        return sum(taxes.itervalues(), _ZERO)

    @classmethod
    def get_amount(cls, sales, names):
        untaxed_amount = {}
        tax_amount = {}
        total_amount = {}

        if {'tax_amount', 'total_amount'} & set(names):
            compute_taxes = True
        else:
            compute_taxes = False
        # Sort cached first and re-instanciate to optimize cache management
        sales = sorted(sales, key=lambda s: s.state in cls._states_cached,
            reverse=True)
        sales = cls.browse(sales)
        for sale in sales:
            if (sale.state in cls._states_cached
                    and sale.untaxed_amount_cache is not None
                    and sale.tax_amount_cache is not None
                    and sale.total_amount_cache is not None):
                untaxed_amount[sale.id] = sale.untaxed_amount_cache
                if compute_taxes:
                    tax_amount[sale.id] = sale.tax_amount_cache
                    total_amount[sale.id] = sale.total_amount_cache
            else:
                untaxed_amount[sale.id] = sum(
                    (line.amount for line in sale.lines
                        if line.type == 'line'), _ZERO)
                if compute_taxes:
                    tax_amount[sale.id] = sale.get_tax_amount()
                    total_amount[sale.id] = (
                        untaxed_amount[sale.id] + tax_amount[sale.id])

        result = {
            'untaxed_amount': untaxed_amount,
            'tax_amount': tax_amount,
            'total_amount': total_amount,
            }
        for key in result.keys():
            if key not in names:
                del result[key]
        return result


    def get_shipments_returns(model_name):
        def method(self, name):
            Model = Pool().get(model_name)
            shipments = set()
            for line in self.lines:
                for move in line.moves:
                    if isinstance(move.shipment, Model):
                        shipments.add(move.shipment.id)
            return list(shipments)
        return method

    get_shipments = get_shipments_returns('stock.shipment.out')
    get_shipment_returns = get_shipments_returns('stock.shipment.out.return')

    def search_shipments_returns(model_name):
        def method(self, name, clause):
            return [('lines.moves.shipment.id',) + tuple(clause[1:])
                + (model_name,)]
        return classmethod(method)

    search_shipments = search_shipments_returns('stock.shipment.out')
    search_shipment_returns = search_shipments_returns(
        'stock.shipment.out.return')

    def get_moves(self, name):
        return [m.id for l in self.lines for m in l.moves]

    def set_number(self):
        sequence_delivery_note = None
        pool = Pool()
        Shop = Pool().get('sale.shop')
        shop = Shop(Transaction().context.get('shop'))

        if shop.sequence_delivery_note:
            sequence_delivery_note = shop.sequence_delivery_note

        if sequence_delivery_note:
            if len(str(sequence_delivery_note)) == 1:
                number = '00000000'+str(sequence_delivery_note)
            elif len(str(sequence_delivery_note)) == 2:
                number = '0000000'+str(sequence_delivery_note)
            elif len(str(sequence_delivery_note)) == 3:
                number = '000000'+str(sequence_delivery_note)
            elif len(str(sequence_delivery_note)) == 4:
                number = '00000'+str(sequence_delivery_note)
            elif len(str(sequence_delivery_note)) == 5:
                number = '0000'+str(sequence_delivery_note)
            elif len(str(sequence_delivery_note)) == 6:
                number = '000'+str(sequence_delivery_note)
            elif len(str(sequence_delivery_note)) == 7:
                number = '00'+str(sequence_delivery_note)
            elif len(str(sequence_delivery_note)) == 8:
                number = '0'+str(sequence_delivery_note)
            elif len(str(sequence_delivery_note)) == 9:
                number = +str(sequence_delivery_note)
            shop.sequence_delivery_note = sequence_delivery_note + 1
            shop.save()
        vals = {'number': number}
        self.write([self], vals)

    @classmethod
    @ModelView.button
    def save(cls, sales):
        Line = Pool().get('sale.delivery_line')
        sale = sales[0]
        shipment_type = 'out'
        sale.create_shipment(shipment_type)
        sale.set_number()
        cls.write(sales, {'state': 'saved'})

    @classmethod
    @ModelView.button_action('nodux_sale_delivery_note.wizard_consolidate')
    #@Workflow.transition('invoiced')
    def consolidate(cls, sales):
        Line = Pool().get('sale.delivery_line')
        sale = sales[0]
        shipment_type = 'return'
        sale.create_shipment(shipment_type)
        cls.write(sales, {'state': 'invoiced'})


    def create_shipment(self, shipment_type):
        return self.create_moves_without_shipment(shipment_type)
        return super(Delivery, self).create_shipment(shipment_type)

    def create_moves_without_shipment(self, shipment_type):
        pool = Pool()
        Move = pool.get('stock.move')
        shipment_method = 'order'

        #sale.create_shipment('return')
        moves = self._get_move_sale_line(shipment_type)
        to_create = []
        for m in moves:
            to_create.append(moves[m]._save_values)

        Move.create(to_create)
        Move.do(self.moves)

    def _get_move_sale_line(self, shipment_type):
        res = {}
        for line in self.lines:
            val = line.get_move(shipment_type)
            if val:
                res[line.id] = val
        return res

class DeliveryLine(ModelSQL, ModelView):
    'Delivery Line'
    __name__ = 'sale.delivery_line'
    _rec_name = 'description'

    delivery = fields.Many2One('sale.delivery', 'Delivery', ondelete='CASCADE',
        select=True)
    sequence = fields.Integer('Sequence')
    type = fields.Selection([
        ('line', 'Line'),
        ], 'Type', select=True, required=True)
    quantity = fields.Float('Quantity',
        digits=(16, Eval('unit_digits', 2)),
        states={
            'invisible': Eval('type') != 'line',
            'required': Eval('type') == 'line',
            'readonly': ~Eval('_parent_delivery', {}),
            },
        depends=['type', 'unit_digits'])
    unit = fields.Many2One('product.uom', 'Unit',
            states={
                'required': Bool(Eval('product')),
                'invisible': Eval('type') != 'line',
                'readonly': ~Eval('_parent_delivery', {}),
            },
        domain=[
            If(Bool(Eval('product_uom_category')),
                ('category', '=', Eval('product_uom_category')),
                ('category', '!=', -1)),
            ],
        depends=['product', 'type', 'product_uom_category'])
    unit_digits = fields.Function(fields.Integer('Unit Digits'),
        'on_change_with_unit_digits')
    product = fields.Many2One('product.product', 'Product',
        domain=[('salable', '=', True)],
        states={
            'invisible': Eval('type') != 'line',
            'readonly': ~Eval('_parent_delivery', {}),
            },
        context={
            'locations': If(Bool(Eval('_parent_delivery', {}).get('warehouse')),
                [Eval('_parent_delivery', {}).get('warehouse', 0)], []),
            'stock_date_end': Eval('_parent_delivery', {}).get('delivery_date'),
            'stock_skip_warehouse': True,
            }, depends=['type'])
    product_uom_category = fields.Function(
        fields.Many2One('product.uom.category', 'Product Uom Category'),
        'on_change_with_product_uom_category')
    unit_price = fields.Numeric('Unit Price', digits=(16, 4),
        states={
            'invisible': Eval('type') != 'line',
            'required': Eval('type') == 'line',
            }, depends=['type'])
    amount = fields.Function(fields.Numeric('Amount',
            digits=(16, Eval('_parent_delivery', {}).get('currency_digits', 2)),
            states={
                'invisible': ~Eval('type').in_(['line', 'subtotal']),
                'readonly': ~Eval('_parent_delivery'),
                },
            depends=['type']), 'get_amount')
    description = fields.Text('Description', size=None, required=True)
    note = fields.Text('Note')
    taxs = fields.Many2Many('sale.delivery_line-account.tax', 'line', 'tax', 'Taxes',
        states={
            'invisible': Eval('type') != 'line',
            })
    moves = fields.One2Many('stock.move', 'origin', 'Moves', readonly=True)
    warehouse = fields.Function(fields.Many2One('stock.location',
            'Warehouse'), 'get_warehouse')
    from_location = fields.Function(fields.Many2One('stock.location',
            'From Location'), 'get_from_location')
    to_location = fields.Function(fields.Many2One('stock.location',
            'To Location'), 'get_to_location')
    delivery_date = fields.Function(fields.Date('Delivery Date',
            states={
                'invisible': ((Eval('type') != 'line')
                    | (If(Bool(Eval('quantity')), Eval('quantity', 0), 0)
                        <= 0)),
                },
            depends=['type', 'quantity']),
        'on_change_with_delivery_date')

    product_type = fields.Function(fields.Char('Product Type'),
        'on_change_with_product_type')

    lot = fields.Many2One('stock.lot', 'Lot',
        domain=[
            ('used_lot', '=', 'no_used'),
            ('product', '=', Eval('product')),

            ],
        states={
            'invisible': ((Eval('type') != 'line')
                | (Eval('product_type') == 'service')),
            },
        depends=['type', 'product', 'product_type', 'used'])

    @fields.depends('product')
    def on_change_with_product_type(self, name=None):
        if not self.product:
            return
        return self.product.type

    @classmethod
    def __setup__(cls):
        super(DeliveryLine, cls).__setup__()
        cls._order.insert(0, ('sequence', 'ASC'))

    @staticmethod
    def order_sequence(tables):
        table, _ = tables[None]
        return [table.sequence == None, table.sequence]

    @staticmethod
    def default_type():
        return 'line'

    @staticmethod
    def default_unit_digits():
        return 2

    @fields.depends('unit')
    def on_change_with_unit_digits(self, name=None):
        if self.unit:
            return self.unit.digits
        return 2

    def _get_context_sale_price(self):
        context = {}
        if getattr(self, 'delivery', None):
            if getattr(self.delivery, 'currency', None):
                context['currency'] = self.delivery.currency.id
            if getattr(self.delivery, 'party', None):
                context['customer'] = self.delivery.party.id
            if getattr(self.delivery, 'delivery_date', None):
                context['delivery_date'] = self.delivery.delivery_date
        if self.unit:
            context['uom'] = self.unit.id
        else:
            context['uom'] = self.product.sale_uom.id
        return context

    @fields.depends('product', 'unit', 'quantity', 'description',
        '_parent_delivery.party', '_parent_delivery.currency',
        '_parent_delivery.delivery_date')
    def on_change_product(self):
        Product = Pool().get('product.product')

        if not self.product:
            return {}
        res = {}

        party = None
        party_context = {}
        if self.delivery and self.delivery.party:
            party = self.delivery.party
            if party.lang:
                party_context['language'] = party.lang.code

        category = self.product.sale_uom.category
        if not self.unit or self.unit not in category.uoms:
            res['unit'] = self.product.sale_uom.id
            self.unit = self.product.sale_uom
            res['unit.rec_name'] = self.product.sale_uom.rec_name
            res['unit_digits'] = self.product.sale_uom.digits

        with Transaction().set_context(self._get_context_sale_price()):
            res['unit_price'] = Product.get_sale_price([self.product],
                    self.quantity or 0)[self.product.id]
            if res['unit_price']:
                res['unit_price'] = res['unit_price'].quantize(
                    Decimal(1) / 10 ** self.__class__.unit_price.digits[1])
        res['taxs'] = []
        pattern = self._get_tax_rule_pattern()
        for tax in self.product.customer_taxes_used:
            if party and party.customer_tax_rule:
                tax_ids = party.customer_tax_rule.apply(tax, pattern)
                if tax_ids:
                    res['taxs'].extend(tax_ids)
                continue
            res['taxs'].append(tax.id)
        if party and party.customer_tax_rule:
            tax_ids = party.customer_tax_rule.apply(None, pattern)
            if tax_ids:
                res['taxs'].extend(tax_ids)
        if not self.description:
            with Transaction().set_context(party_context):
                res['description'] = Product(self.product.id).rec_name

        self.unit_price = res['unit_price']
        self.type = 'line'
        res['amount'] = self.on_change_with_amount()
        return res

    def _get_tax_rule_pattern(self):
        '''
        Get tax rule pattern
        '''
        return {}

    @fields.depends('product')
    def on_change_with_product_uom_category(self, name=None):
        if self.product:
            return self.product.default_uom_category.id

    @fields.depends('product', 'quantity', 'unit',
        '_parent_delivery.currency', '_parent_delivery.party',
        '_parent_delivery.delivery_date')
    def on_change_quantity(self):
        Product = Pool().get('product.product')

        if not self.product:
            return {}
        res = {}

        with Transaction().set_context(
                self._get_context_sale_price()):
            res['unit_price'] = Product.get_sale_price([self.product],
                self.quantity or 0)[self.product.id]
            if res['unit_price']:
                res['unit_price'] = res['unit_price'].quantize(
                    Decimal(1) / 10 ** self.__class__.unit_price.digits[1])
        return res

    @fields.depends('product', 'quantity', 'unit',
        '_parent_delivery.currency', '_parent_delivery.party',
        '_parent_delivery.delivery_date')
    def on_change_unit(self):
        return self.on_change_quantity()

    @fields.depends('type', 'quantity', 'unit_price', 'unit',
        '_parent_delivery.currency')
    def on_change_with_amount(self):
        if self.type == 'line':
            currency = self.delivery.currency if self.delivery else None
            amount = Decimal(str(self.quantity or '0.0')) * \
                (self.unit_price or Decimal('0.0'))
            if currency:
                return currency.round(amount)
            return amount
        return Decimal('0.0')

    def get_amount(self, name):
        if self.type == 'line':
            return self.on_change_with_amount()

        return Decimal('0.0')

    def get_warehouse(self, name):
        return self.delivery.warehouse.id if self.delivery.warehouse else None

    def get_from_location(self, name):
        if self.quantity >= 0:
            if self.warehouse:
                return self.warehouse.output_location.id
        else:
            return self.delivery.party.customer_location.id

    def get_to_location(self, name):
        if self.quantity >= 0:
            return self.delivery.party.customer_location.id
        else:
            if self.warehouse:
                return self.warehouse.input_location.id

    @fields.depends('product', 'quantity', '_parent_delivery.delivery_date')
    def on_change_with_delivery_date(self, name=None):
        if self.product and self.quantity > 0:
            date = self.delivery.delivery_date if self.delivery else None
            return self.product.compute_delivery_date(date=date)

    def get_move(self, shipment_type):
        pool = Pool()
        Uom = pool.get('product.uom')
        Move = pool.get('stock.move')

        quantity = self.quantity

        if self.type != 'line':
            return
        if not self.product:
            return
        if self.product.type == 'service':
            return
        """
        if (shipment_type == 'out') != (self.quantity >= 0):
            return
        """
        if not self.delivery.party.customer_location:
            self.raise_user_error('customer_location_required', {
                    'sale': self.delivery.rec_name,
                    'line': self.rec_name,
                    })

        move = Move()
        move.quantity = quantity
        move.uom = self.unit
        move.product = self.product
        if shipment_type == "return":
            move.to_location = self.from_location
            move.from_location = self.to_location
            if self.lot:
                move.lot = self.lot
                lot = self.lot
                lot.used_lot = 'no_used'
                lot.save()
            else:
                self.raise_user_error('Se requiere el lote del producto')

        else:
            move.from_location = self.from_location
            move.to_location = self.to_location
            if self.lot:
                move.lot = self.lot
                lot = self.lot
                lot.used_lot = 'used'
                lot.save()
            else:
                self.raise_user_error('Se requiere el lote del producto')

        move.state = 'draft'
        move.company = self.delivery.company
        move.unit_price = self.unit_price
        move.currency = self.delivery.currency
        move.planned_date = self.delivery_date
        move.origin = self

        move.save()

class DeliveryLineTax(ModelSQL):
    'Delivery Line - Tax'
    __name__ = 'sale.delivery_line-account.tax'
    _table = 'delivery_line_account_tax'
    line = fields.Many2One('sale.delivery_line', 'Delivery Line', ondelete='CASCADE',
            select=True, required=True)
    tax = fields.Many2One('account.tax', 'Tax', ondelete='RESTRICT',
            select=True, required=True)


class ValidatedInvoice(Wizard):
    'Consolidate Invoice'
    __name__ = 'sale.consolidate_invoice'

    start = StateView('sale.sale',
        'sale_pos.sale_pos_view_form', [
            Button('Cerrar', 'end', 'tryton-ok', default=True),
            ])

    def default_start(self, fields):

        Delivery = Pool().get('sale.delivery')
        Date = Pool().get('ir.date')
        fecha_actual = Date.today()

        default = {}

        delivery = Delivery(Transaction().context.get('active_id'))

        default['company'] = delivery.company.id
        default['state'] = 'draft'
        default['sale_date'] = fecha_actual
        default['party'] = delivery.party.id
        default['currency']=delivery.currency.id
        default['warehouse']= delivery.warehouse.id
        default['lines']=[]
        default['untaxed_amount'] = delivery.untaxed_amount
        default['tax_amount'] = delivery.tax_amount
        default['total_amount'] = delivery.total_amount
        if delivery.lines:
            for line in delivery.lines:
                lines = {
                    'type' : 'line',
                    'quantity': line.quantity,
                    'unit':line.unit.id,
                    'product': line.product.id,
                    'unit_price': line.unit_price,
                    'amount':line.amount,
                    'description': line.description,
                    'lot': line.lot.id,
                    }
                default['lines'].append(lines)
        return default


class DeliveryNoteReport(Report):
    __name__ = 'sale.delivery_report'

    @classmethod
    def parse(cls, report, records, data, localcontext):
        pool = Pool()
        User = pool.get('res.user')
        Delivery = pool.get('sale.delivery')

        delivery = records[0]
        User = pool.get('res.user')
        user = User(Transaction().user)


        d = str(delivery.total_amount)
        if '.' in d:
            decimales = d[-2:]
            if decimales[0] == '.':
                 decimales = decimales[1]+'0'
        else:
            decimales = '00'

        localcontext['company'] = user.company
        localcontext['delivery'] = delivery
        localcontext['subtotal_0'] = cls._get_subtotal_0(Delivery, delivery)
        localcontext['subtotal_12'] = cls._get_subtotal_12(Delivery, delivery)
        localcontext['subtotal_14'] = cls._get_subtotal_14(Delivery, delivery)
        localcontext['amount2words']=cls._get_amount_to_pay_words(Delivery, delivery)
        localcontext['decimales'] = decimales
        localcontext['descuento'] = Decimal(0.0)
        return super(DeliveryNoteReport, cls).parse(report, records, data,
                localcontext=localcontext)

    @classmethod
    def _get_amount_to_pay_words(cls, Delivery, delivery):
        amount_to_pay_words = ""
        if delivery.total_amount and conversor:
            amount_to_pay_words = delivery.get_amount2words(delivery.total_amount)
        return amount_to_pay_words

    @classmethod
    def _get_subtotal_12(cls, Delivery, delivery):
        subtotal12 = Decimal(0.00)
        pool = Pool()

        for line in delivery.lines:
            if  line.taxs:
                for t in line.taxs:
                    if str('{:.0f}'.format(t.rate*100)) == '12':
                        subtotal12= subtotal12 + (line.amount)
        if subtotal12 < 0:
            subtotal12 = subtotal12*(-1)
        return subtotal12

    @classmethod
    def _get_subtotal_14(cls, Delivery, delivery):
        subtotal14 = Decimal(0.00)
        pool = Pool()

        for line in delivery.lines:
            if  line.taxs:
                for t in line.taxs:
                    if str('{:.0f}'.format(t.rate*100)) == '14':
                        subtotal14= subtotal14 + (line.amount)
        if subtotal14 < 0:
            subtotal14 = subtotal14*(-1)
        return subtotal14

    @classmethod
    def _get_subtotal_0(cls, Delivery, delivery):
        subtotal0 = Decimal(0.00)
        pool = Pool()

        for line in delivery.lines:
            if  line.taxs:
                for t in line.taxs:
                    if str('{:.0f}'.format(t.rate*100)) == '0':
                        subtotal0= subtotal0 + (line.amount)
        if subtotal0 < 0:
            subtotal0 = subtotal0*(-1)
        return subtotal0
