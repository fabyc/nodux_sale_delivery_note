#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
from trytond.pool import PoolMeta, Pool

__all__ = ['Move']
__metaclass__ = PoolMeta

class Move:
    __name__ = 'stock.move'

    @staticmethod
    def _get_origin():
        return ['stock.inventory.line', 'sale.delivery_line', 'sale.line']

    @classmethod
    def get_origin(cls):
        IrModel = Pool().get('ir.model')
        models = cls._get_origin()
        models = IrModel.search([
                ('model', 'in', models),
                ])
        return [(None, '')] + [(m.model, m.name) for m in models]
