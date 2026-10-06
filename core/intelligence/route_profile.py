"""Stock route profiles used to select intelligence collection fields."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.intelligence.source_catalog import REQUIRED_FIELDS


@dataclass(frozen=True)
class RouteProfile:
    code: str
    name: str
    template_id: str
    sectors: tuple[str, ...]
    keywords: tuple[str, ...]
    field_routes: dict[str, tuple[str, ...]]

    def queries_for_field(self, field: str) -> tuple[str, ...]:
        return tuple(self.field_routes.get(field, ()))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['field_routes'] = {key: list(value) for key, value in self.field_routes.items()}
        return data


def build_route_profile(
    code: str,
    name: str,
    context: dict[str, Any] | None = None,
) -> RouteProfile:
    clean_code = _clean_code(code)
    if clean_code in {'300308', '300502'}:
        return _cpo_profile(clean_code, name or clean_code)
    if clean_code == '600584':
        return _semiconductor_profile(clean_code, name or clean_code)
    if clean_code == '000681':
        return _copyright_profile(clean_code, name or clean_code)
    return _generic_profile(clean_code, name or clean_code, context)


def _cpo_profile(code: str, name: str) -> RouteProfile:
    keywords = _unique((
        name, code, 'CPO', '光模块', '800G', '1.6T', 'AI算力', '算力',
        '数据中心', '海外客户', '云厂商', '光通信', '硅光',
        'optical module', 'optical transceiver', 'AI data center',
        'cloud customer', 'overseas customer',
    ))
    return RouteProfile(
        code=code,
        name=name,
        template_id='cpo_optical_module',
        sectors=('optical communication', 'CPO', 'AI datacenter'),
        keywords=keywords,
        field_routes=_routes({
            'macro': ('AI data center', 'AI算力', '数据中心'),
            'policy': ('光模块', '800G', '人工智能 信息通信'),
            'company_disclosure': (name, code),
            'financial': (name, code),
            'cost': ('光芯片 价格', '光器件 成本', '硅光 成本'),
            'demand': ('AI算力', '数据中心', '800G', '1.6T'),
            'export': ('海外客户', 'overseas customer', 'export'),
            'customer_supplier': ('海外客户', '云厂商', 'customer', 'supplier'),
            'inventory': ('补库', '库存', 'restocking'),
            'capacity': ('产能', '扩产', '生产建设项目'),
            'competition': ('CPO', '800G', '1.6T', '硅光', 'competition'),
            'order_contract': (name, code, '合同', '订单', '采购'),
            'trading_behavior': (name, code),
            'risk': (name, code, '风险', '诉讼'),
            'news_sentiment': (name, code, 'CPO', '光模块'),
        }),
    )


def _semiconductor_profile(code: str, name: str) -> RouteProfile:
    keywords = _unique((
        name, code, '半导体封测', '先进封装', 'Chiplet', '存储',
        '消费电子', '汽车电子', '集成电路', '封装材料',
        'advanced packaging', 'semiconductor packaging', 'semiconductor', 'OSAT',
    ))
    return RouteProfile(
        code=code,
        name=name,
        template_id='semiconductor_packaging',
        sectors=('semiconductor', 'advanced packaging', 'OSAT'),
        keywords=keywords,
        field_routes=_routes({
            'macro': ('semiconductor cycle', 'AI chip demand', '半导体'),
            'policy': ('半导体', '先进封装', '集成电路'),
            'company_disclosure': (name, code),
            'financial': (name, code),
            'cost': ('先进封装 成本', '封装材料', '半导体 封装 成本'),
            'demand': ('AI芯片', '汽车电子', '存储', '消费电子'),
            'export': ('海外客户', 'export', 'global customer'),
            'customer_supplier': ('客户', '供应商', '供应链', 'OSAT'),
            'inventory': ('库存', '去库', '补库', '存储'),
            'capacity': ('先进封装 产能', '扩产', '生产基地'),
            'competition': ('先进封装', 'Chiplet', 'OSAT', 'market share'),
            'order_contract': (name, code, '合同', '订单', '中标'),
            'trading_behavior': (name, code),
            'risk': (name, code, '减值', '诉讼', '处罚'),
            'news_sentiment': (name, code, '先进封装', '半导体'),
        }),
    )


def _copyright_profile(code: str, name: str) -> RouteProfile:
    keywords = _unique((
        name, code, '版权', 'AI语料', '图片版权', 'AIGC',
        '内容授权', '诉讼风险', '重点作品版权保护',
        'copyright', 'image licensing', 'content licensing',
    ))
    return RouteProfile(
        code=code,
        name=name,
        template_id='copyright_aigc',
        sectors=('copyright', 'AIGC', 'visual content'),
        keywords=keywords,
        field_routes=_routes({
            'macro': ('AIGC copyright', 'AI training data', '内容授权'),
            'policy': ('版权', 'AIGC', '人工智能'),
            'company_disclosure': (name, code),
            'financial': (name, code),
            'cost': ('内容成本', '版权采购', 'licensing cost'),
            'demand': ('AI语料', '内容授权', '图片版权需求'),
            'export': ('海外授权', 'global licensing', 'export'),
            'customer_supplier': ('客户', '授权客户', '内容供应商'),
            'inventory': ('素材库', '版权库', '内容库存'),
            'capacity': ('版权库建设', '数据集建设', '平台能力'),
            'competition': ('AIGC', '图库', '版权竞争'),
            'order_contract': (name, code, '合同', '授权', '订单'),
            'trading_behavior': (name, code),
            'risk': (name, code, '诉讼风险', '版权纠纷', '监管'),
            'news_sentiment': (name, code, 'AIGC', '版权'),
        }),
    )


def _generic_profile(code: str, name: str, context: dict[str, Any] | None) -> RouteProfile:
    sectors = _context_sectors(context)
    keywords = _unique((name, code, *sectors))
    routes = {field: keywords[:4] for field in REQUIRED_FIELDS}
    return RouteProfile(
        code=code,
        name=name,
        template_id='generic',
        sectors=sectors,
        keywords=keywords,
        field_routes=routes,
    )


def _routes(values: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    return {field: _unique(values.get(field, ())) for field in REQUIRED_FIELDS}


def _context_sectors(context: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(context, dict):
        return ()
    value = context.get('sectors')
    if not isinstance(value, (list, tuple)):
        inner = context.get('context') if isinstance(context.get('context'), dict) else {}
        value = inner.get('sectors') if isinstance(inner.get('sectors'), (list, tuple)) else ()
    return _unique(tuple(str(item).strip() for item in value if str(item).strip()))


def _clean_code(code: str) -> str:
    return ''.join(ch for ch in str(code or '') if ch.isdigit())[-6:]


def _unique(values) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = str(value or '').strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)
