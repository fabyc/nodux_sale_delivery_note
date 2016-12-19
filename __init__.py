# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.

from trytond.pool import Pool
from .delivery import *
from .move import *

def register():
    Pool.register(
        Delivery,
        DeliveryLine,
        DeliveryLineTax,
        Move,
        module='nodux_sale_delivery_note', type_='model')
    Pool.register(
        ValidatedInvoice,
        module='nodux_sale_delivery_note', type_='wizard')
