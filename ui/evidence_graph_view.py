"""Embedded EvidenceGraph view with WebEngine and 2.5D fallback."""
from __future__ import annotations

import json
import math
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from core.qt_runtime import configure_qt_runtime
configure_qt_runtime()

from PySide6.QtCore import QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QBrush, QLinearGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QStackedLayout, QWidget


_ASSET_DIR = Path(__file__).resolve().parent / 'assets' / 'evidence_graph'
_HTML_PATH = _ASSET_DIR / 'graph.html'

_LAYER_COLORS = {
    'company': '#f7fbff',
    'sector': '#4fc3ff',
    'macro': '#2d6cdf',
    'policy': '#a76dff',
    'news_event': '#ff416d',
    'financial': '#ffd166',
    'cost': '#ff9f43',
    'demand': '#2ee6a6',
    'export': '#7b8cff',
    'geopolitics_trade': '#7b8cff',
    'customer_supplier': '#46e0d2',
    'trading_behavior': '#31f28a',
    'risk': '#ff4d5e',
    'missing': '#6f7f99',
    'expectation': '#bffcff',
    'forecast': '#bffcff',
}

_LAYER_ANCHORS = {
    'sector': (-0.70, 0.18, 0.68),
    'macro': (-0.12, 0.82, -0.54),
    'policy': (0.40, 0.72, -0.42),
    'financial': (-0.36, -0.54, 0.76),
    'shareholder': (-0.50, -0.48, 0.62),
    'trading_behavior': (0.72, -0.24, 0.64),
    'risk': (-0.70, -0.18, -0.66),
    'cost': (-0.80, -0.10, 0.54),
    'price': (-0.76, 0.02, 0.62),
    'inventory': (-0.74, 0.10, 0.58),
    'capacity': (-0.62, 0.02, 0.72),
    'competition': (-0.78, 0.22, 0.50),
    'demand': (0.04, -0.76, 0.64),
    'export': (0.54, 0.08, -0.82),
    'geopolitics_trade': (0.50, 0.20, -0.84),
    'customer_supplier': (-0.20, -0.78, 0.58),
    'missing': (-0.28, -0.82, -0.50),
    'expectation': (0.18, 0.08, 0.98),
    'forecast': (0.18, 0.08, 0.98),
    'news_event': (0.56, 0.46, 0.68),
    'company': (0.0, 0.0, 1.0),
}

_CLUSTER_BY_LAYER = {
    'macro': 'external',
    'policy': 'external',
    'geopolitics_trade': 'external',
    'liquidity': 'external',
    'sector': 'industry',
    'cost': 'industry',
    'price': 'industry',
    'inventory': 'industry',
    'capacity': 'industry',
    'competition': 'industry',
    'company': 'company',
    'financial': 'company',
    'order_contract': 'company',
    'shareholder': 'company',
    'trading_behavior': 'trading',
    'news_event': 'news',
    'risk': 'risk',
    'missing': 'missing',
    'expectation': 'forecast',
    'forecast': 'forecast',
    'demand': 'external',
    'export': 'external',
    'customer_supplier': 'company',
}

_VISUAL_MODE = {
    '效率': (72, 96),
    '性能': (340, 560),
}

_CAMERA_BY_MODE = {
    '效率': (610, 0.66),
    '性能': (455, 0.82),
}


class _FallbackGraphCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._graph: dict[str, Any] = {'nodes': [], 'links': [], 'meta': {}}
        self._focus_layers: set[str] = set()
        self.setMouseTracking(True)
        self.setMinimumSize(360, 260)

    def set_graph(self, graph: dict[str, Any]) -> None:
        self._graph = graph if isinstance(graph, dict) else {'nodes': [], 'links': [], 'meta': {}}
        self.update()

    def focus_layers(self, layers: list[str] | tuple[str, ...] | set[str]) -> None:
        self._focus_layers = {str(layer) for layer in layers if str(layer)}
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        gradient = QLinearGradient(0, 0, rect.width(), rect.height())
        gradient.setColorAt(0.0, QColor('#07101f'))
        gradient.setColorAt(0.58, QColor('#0c1730'))
        gradient.setColorAt(1.0, QColor('#111c38'))
        painter.fillRect(rect, QBrush(gradient))

        core = QRadialGradient(rect.center(), min(rect.width(), rect.height()) * 0.34)
        core.setColorAt(0.0, QColor(64, 195, 255, 54))
        core.setColorAt(0.55, QColor(17, 28, 56, 30))
        core.setColorAt(1.0, QColor(7, 16, 31, 0))
        painter.fillRect(rect, QBrush(core))

        nodes = list(self._graph.get('nodes') or [])
        links = list(self._graph.get('links') or [])
        positions = self._positions(nodes)

        for link in sorted(links, key=lambda item: 0 if item.get('visual_only') else 1):
            source = link.get('source')
            target = link.get('target')
            if isinstance(source, dict):
                source = source.get('id')
            if isinstance(target, dict):
                target = target.get('id')
            if source not in positions or target not in positions:
                continue
            layer = str(link.get('layer') or '')
            focused = not self._focus_layers or layer in self._focus_layers
            color = QColor(str(link.get('color') or '#2a9df4'))
            if link.get('visual_only'):
                color.setAlpha(42 if focused else 16)
                width = 0.35
            else:
                color.setAlpha(150 if focused else 42)
                width = 1.4 if focused else 0.55
            painter.setPen(QPen(color, width))
            painter.drawLine(positions[source], positions[target])

        for node in sorted(nodes, key=lambda item: (not item.get('visual_only'), float(item.get('val') or 1))):
            node_id = str(node.get('id') or '')
            if node_id not in positions:
                continue
            layer = str(node.get('layer') or '')
            focused = not self._focus_layers or layer in self._focus_layers or node.get('type') == 'stock'
            val = max(0.8, min(14.0, float(node.get('val') or 3)))
            point = positions[node_id]
            color = QColor(str(node.get('color') or '#93a4c8'))
            halo = QColor(color)
            painter.setPen(Qt.PenStyle.NoPen)
            if node.get('visual_only'):
                color.setAlpha(92 if focused else 28)
                painter.setBrush(color)
                painter.drawEllipse(point, val * 0.45, val * 0.45)
                continue

            if node.get('visual_role') in {'stock', 'strong', 'risk', 'missing', 'forecast'}:
                halo.setAlpha(76 if focused else 14)
                painter.setBrush(halo)
                painter.drawEllipse(point, val * 1.55, val * 1.55)
            color.setAlpha(246 if focused else 62)
            painter.setBrush(color)
            painter.drawEllipse(point, val * 0.45, val * 0.45)

            if focused and node.get('type') == 'stock':
                painter.setPen(QPen(QColor('#d9e6ff'), 1))
                painter.drawText(
                    int(point.x() + val * 0.7),
                    int(point.y() + 4),
                    str(node.get('name') or '')[:16],
                )

        painter.end()

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        nodes = list(self._graph.get('nodes') or [])
        positions = self._positions(nodes)
        pos = event.position()
        best = None
        best_dist = 9999.0
        for node in nodes:
            if node.get('visual_only'):
                continue
            point = positions.get(str(node.get('id') or ''))
            if point is None:
                continue
            dist = math.hypot(point.x() - pos.x(), point.y() - pos.y())
            if dist < best_dist:
                best = node
                best_dist = dist
        if best and best_dist <= 18:
            self.setToolTip(f"{best.get('name', '')}\n{best.get('summary', '')}")
        else:
            self.setToolTip('')

    def _positions(self, nodes: list[dict[str, Any]]) -> dict[str, QPointF]:
        w = max(1, self.width())
        h = max(1, self.height())
        cx = w * 0.5
        cy = h * 0.52
        scale = min(w, h) * 0.42 / 260.0
        positions: dict[str, QPointF] = {}
        non_stock: list[dict[str, Any]] = []
        for node in nodes:
            node_id = str(node.get('id') or '')
            if node.get('x') is not None and node.get('y') is not None:
                z = float(node.get('z') or 0)
                perspective = 1.0 + z / 900.0
                positions[node_id] = QPointF(
                    cx + float(node.get('x') or 0) * scale * perspective,
                    cy + float(node.get('y') or 0) * scale * perspective,
                )
                continue
            if node.get('type') == 'stock':
                positions[node_id] = QPointF(cx, cy)
            else:
                non_stock.append(node)

        for idx, node in enumerate(non_stock):
            node_id = str(node.get('id') or '')
            layer = str(node.get('layer') or '')
            ring = _layer_ring(layer)
            angle = (idx * 2.399963229728653 + ring * 0.41) % (math.pi * 2)
            radius = scale * (0.18 + ring * 0.07)
            positions[node_id] = QPointF(cx + math.cos(angle) * radius, cy + math.sin(angle) * radius)
        return positions


class EvidenceGraphView(QWidget):
    """Widget API used by the intelligence panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._graph: dict[str, Any] = {'nodes': [], 'links': [], 'meta': {}}
        self._focus_layers: list[str] = []
        self._web_loaded = False
        self._web = None
        self._fallback = _FallbackGraphCanvas(self)
        self._layout = QStackedLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(self._fallback)
        self._try_webengine()

    @property
    def using_webengine(self) -> bool:
        return self._web is not None

    def set_graph(self, graph: dict[str, Any]) -> None:
        raw_graph = graph if isinstance(graph, dict) else {'nodes': [], 'links': [], 'meta': {}}
        mode = str((raw_graph.get('meta') or {}).get('performance_mode') or '性能')
        self._graph = self.prepare_visual_graph(raw_graph, mode)
        self._fallback.set_graph(self._graph)
        self._push_graph_to_web()

    def focus_layers(self, layers: list[str] | tuple[str, ...] | set[str]) -> None:
        self._focus_layers = [str(layer) for layer in layers if str(layer)]
        self._fallback.focus_layers(self._focus_layers)
        if self._web and self._web_loaded:
            payload = json.dumps(self._focus_layers, ensure_ascii=False)
            self._web.page().runJavaScript(
                f'window.AldebaranGraph && window.AldebaranGraph.focusLayers({payload});'
            )

    def _try_webengine(self) -> None:
        if not _HTML_PATH.exists():
            return
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            web = QWebEngineView(self)
            web.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            web.loadFinished.connect(self._on_web_loaded)
            web.setUrl(QUrl.fromLocalFile(str(_HTML_PATH)))
            self._web = web
            self._layout.addWidget(web)
            self._layout.setCurrentWidget(web)
        except Exception:
            self._web = None
            self._layout.setCurrentWidget(self._fallback)

    def _on_web_loaded(self, ok: bool) -> None:
        self._web_loaded = bool(ok)
        self._push_graph_to_web()
        if self._focus_layers:
            self.focus_layers(self._focus_layers)

    def _push_graph_to_web(self) -> None:
        if not self._web or not self._web_loaded:
            return
        payload = json.dumps(self._graph, ensure_ascii=False)
        self._web.page().runJavaScript(
            f'window.AldebaranGraph && window.AldebaranGraph.setGraph({payload});'
        )

    @staticmethod
    def prepare_visual_graph(graph: dict[str, Any], performance_mode: str = '性能') -> dict[str, Any]:
        """Create a render-only 3D intelligence globe without changing evidence counts."""
        source_graph = graph if isinstance(graph, dict) else {'nodes': [], 'links': [], 'meta': {}}
        meta = dict(source_graph.get('meta') or {})
        mode = performance_mode if performance_mode in _VISUAL_MODE else str(meta.get('performance_mode') or '性能')
        mode = mode if mode in _VISUAL_MODE else '性能'

        real_nodes = [
            deepcopy(node) for node in (source_graph.get('nodes') or [])
            if isinstance(node, dict) and not node.get('visual_only') and not str(node.get('id') or '').startswith('visual:')
        ]
        real_links = [
            deepcopy(link) for link in (source_graph.get('links') or [])
            if isinstance(link, dict) and not link.get('visual_only') and not str(link.get('id') or '').startswith('visual:')
        ]
        if not real_nodes:
            meta.update({'performance_mode': mode, 'visual_node_count': 0, 'visual_link_count': 0})
            return {'nodes': [], 'links': [], 'meta': meta}

        seed = _stable_seed(
            str(meta.get('subject_code') or ''),
            str(meta.get('subject_name') or ''),
            ''.join(str(node.get('id') or '') for node in real_nodes),
        )
        stock_id = _find_stock_id(real_nodes)
        direct_ids = _direct_neighbors(stock_id, real_links)

        arranged_nodes = _arrange_real_nodes(real_nodes, stock_id, direct_ids, seed)
        arranged_links = _style_real_links(real_links, stock_id)

        visual_node_count, visual_link_count = _VISUAL_MODE[mode]
        camera_distance, screen_ratio = _CAMERA_BY_MODE[mode]
        particles = _build_visual_particles(visual_node_count, arranged_nodes, seed)
        visual_links = _build_visual_links(visual_link_count, arranged_nodes, particles, stock_id, seed)

        meta.update({
            'performance_mode': mode,
            'camera_distance': camera_distance,
            'camera_target_y': 12,
            'camera_target_z': 18,
            'globe_screen_ratio': screen_ratio,
            'node_count': int(meta.get('node_count') or len(arranged_nodes)),
            'link_count': int(meta.get('link_count') or len(arranged_links)),
            'real_node_count': len(arranged_nodes),
            'real_link_count': len(arranged_links),
            'visual_node_count': len(particles),
            'visual_link_count': len(visual_links),
            'render_node_count': len(arranged_nodes) + len(particles),
            'render_link_count': len(arranged_links) + len(visual_links),
        })
        return {
            'nodes': arranged_nodes + particles,
            'links': arranged_links + visual_links,
            'meta': meta,
        }


def _layer_ring(layer: str) -> int:
    ring_map = {
        'sector': 1,
        'financial': 2,
        'trading_behavior': 2,
        'news_event': 3,
        'policy': 3,
        'macro': 4,
        'risk': 4,
        'cost': 4,
        'demand': 4,
        'export': 4,
        'customer_supplier': 5,
        'missing': 5,
    }
    return ring_map.get(layer, 3)


def _arrange_real_nodes(
    nodes: list[dict[str, Any]],
    stock_id: str,
    direct_ids: set[str],
    seed: int,
) -> list[dict[str, Any]]:
    by_layer_index: dict[str, int] = {}
    arranged = []
    for index, node in enumerate(nodes):
        node_id = str(node.get('id') or f'node:{index}')
        node['id'] = node_id
        node_type = str(node.get('type') or '')
        layer = _canonical_layer(str(node.get('layer') or ''), node)
        cluster = _cluster_for_layer(layer)
        node['layer'] = layer
        node['color'] = _node_color(layer, node)
        node['clickable'] = True
        node['visual_only'] = False
        node['fx'] = node['fy'] = node['fz'] = None
        node['val'] = _node_size(node)
        node['visual_role'] = _node_role(node)
        node['cluster_key'] = cluster

        if node_id == stock_id or node_type == 'stock':
            x, y, z = 0.0, 10.0, 34.0
            node['visual_role'] = 'stock'
            node['val'] = min(14.0, max(10.0, float(node.get('val') or 12.0)))
        else:
            layer_index = by_layer_index.get(layer, 0)
            by_layer_index[layer] = layer_index + 1
            anchor = _anchor_for_layer(layer)
            primary = node_id in direct_ids or layer in {
                'sector', 'macro', 'policy', 'financial',
                'trading_behavior', 'risk', 'missing', 'forecast', 'expectation',
            }
            base_radius = 116.0 if primary else 180.0
            radius = base_radius + _rand(seed, index, 1) * (36.0 if primary else 54.0)
            spread = 0.30 + min(layer_index, 10) * 0.055
            jitter = _jitter_vector(seed, index, layer_index, spread)
            direction = _normalize((
                anchor[0] + jitter[0],
                anchor[1] + jitter[1],
                anchor[2] + jitter[2],
            ))
            x, y, z = direction[0] * radius, direction[1] * radius, direction[2] * radius
        node['x'], node['y'], node['z'] = round(x, 3), round(y, 3), round(z, 3)
        node['fx'], node['fy'], node['fz'] = node['x'], node['y'], node['z']
        arranged.append(node)
    return arranged


def _style_real_links(links: list[dict[str, Any]], stock_id: str) -> list[dict[str, Any]]:
    styled = []
    for index, link in enumerate(links):
        source = _endpoint_id(link.get('source'))
        target = _endpoint_id(link.get('target'))
        if not source or not target:
            continue
        layer = _canonical_layer(str(link.get('layer') or ''), {})
        primary = source == stock_id or target == stock_id
        link['id'] = str(link.get('id') or f'evidence-link:{index}')
        link['source'] = source
        link['target'] = target
        link['layer'] = layer
        link['type'] = str(link.get('type') or 'evidence_link')
        link['visual_only'] = False
        link['is_primary'] = primary
        link['color'] = _link_color(layer, link)
        link['opacity'] = 0.36 if primary else 0.24
        link['width'] = max(0.55, min(2.2, float(link.get('value') or link.get('display_weight') or 1.0) * (0.45 if primary else 0.32)))
        link['curvature'] = round((0.048 if primary else 0.018) + ((index % 7) - 3) * 0.006, 3)
        styled.append(link)
    return styled


def _build_visual_particles(
    count: int,
    real_nodes: list[dict[str, Any]],
    seed: int,
) -> list[dict[str, Any]]:
    layers = [str(node.get('layer') or 'macro') for node in real_nodes if node.get('type') != 'stock']
    if not layers:
        layers = ['sector', 'macro', 'financial', 'trading_behavior', 'risk', 'missing']
    particles = []
    for index in range(count):
        layer = layers[index % len(layers)]
        shell_band = index % 8
        shell = 204.0 + shell_band * 10.5 + _rand(seed, index, 7) * 18.0
        direction = _fibonacci_direction(index, count, seed)
        cluster = _anchor_for_layer(layer)
        blend = 0.14 + _rand(seed, index, 8) * 0.15
        direction = _normalize((
            direction[0] * (1.0 - blend) + cluster[0] * blend,
            direction[1] * (1.0 - blend) + cluster[1] * blend,
            direction[2] * (1.0 - blend) + cluster[2] * blend,
        ))
        val = 0.8 + _rand(seed, index, 9) * 0.7
        z = direction[2] * shell
        depth = max(0.0, min(1.0, (z + shell) / (2.0 * shell)))
        particles.append({
            'id': f'visual:particle:{index}',
            'name': '',
            'type': 'visual_particle',
            'layer': layer,
            'cluster_key': _cluster_for_layer(layer),
            'visual_role': 'particle',
            'visual_only': True,
            'clickable': False,
            'val': round(val, 3),
            'color': _visual_shell_color(layer),
            'opacity': round(0.10 + depth * 0.22, 3),
            'depth_alpha': round(0.10 + depth * 0.24, 3),
            'shell_band': shell_band,
            'x': round(direction[0] * shell, 3),
            'y': round(direction[1] * shell, 3),
            'z': round(z, 3),
        })
    for particle in particles:
        particle['fx'], particle['fy'], particle['fz'] = particle['x'], particle['y'], particle['z']
    return particles


def _build_visual_links(
    count: int,
    real_nodes: list[dict[str, Any]],
    particles: list[dict[str, Any]],
    stock_id: str,
    seed: int,
) -> list[dict[str, Any]]:
    real_by_layer: dict[str, list[dict[str, Any]]] = {}
    for node in real_nodes:
        if node.get('id') == stock_id:
            continue
        real_by_layer.setdefault(str(node.get('layer') or 'macro'), []).append(node)
    real_non_stock = [node for node in real_nodes if node.get('id') != stock_id]
    if not particles:
        return []
    positions = {str(node.get('id')): node for node in real_nodes + particles}

    links = []
    layer_particles: dict[str, list[dict[str, Any]]] = {}
    for particle in particles:
        layer_particles.setdefault(str(particle.get('layer') or 'macro'), []).append(particle)

    def add(source_id: str, target_id: str, layer: str, color: str = '#2a9df4') -> None:
        if len(links) >= count or not source_id or not target_id or source_id == target_id:
            return
        index = len(links)
        source = positions.get(source_id, {})
        target = positions.get(target_id, {})
        z_avg = (float(source.get('z') or 0) + float(target.get('z') or 0)) * 0.5
        depth = max(0.0, min(1.0, (z_avg + 275.0) / 550.0))
        links.append({
            'id': f'visual:link:{index}',
            'source': source_id,
            'target': target_id,
            'type': 'visual_link',
            'layer': layer,
            'visual_only': True,
            'clickable': False,
            'color': color,
            'opacity': round(0.10 + depth * 0.11 + _rand(seed, index, 19) * 0.035, 3),
            'depth_alpha': round(0.12 + depth * 0.18, 3),
            'width': 0.16 + _rand(seed, index, 23) * 0.22,
            'value': 0.18,
        })

    for node in real_non_stock:
        layer = str(node.get('layer') or 'macro')
        same_layer = layer_particles.get(layer) or particles
        nearest = sorted(
            same_layer,
            key=lambda particle: _distance_sq(node, particle),
        )[:4]
        for particle in nearest:
            add(str(node.get('id')), str(particle.get('id')), layer, _visual_shell_color(layer))
        if stock_id and len(links) < count and len(links) % 3 == 0:
            add(stock_id, str(node.get('id')), layer, _visual_shell_color(layer))

    bridge_candidates = [
        node for node in real_non_stock
        if str(node.get('cluster_key')) in {'trading', 'news', 'risk', 'missing'}
    ]
    for index, source in enumerate(bridge_candidates):
        for target in bridge_candidates[index + 1:index + 4]:
            if source.get('cluster_key') != target.get('cluster_key'):
                add(str(source.get('id')), str(target.get('id')), str(source.get('layer') or 'macro'), '#2a9df4')

    for layer, items in layer_particles.items():
        ordered = sorted(items, key=lambda particle: (
            float(particle.get('z') or 0),
            float(particle.get('x') or 0),
            float(particle.get('y') or 0),
        ))
        for offset in (1, 2, 4):
            for index, particle in enumerate(ordered):
                if len(links) >= count:
                    break
                target = ordered[(index + offset) % len(ordered)]
                add(str(particle.get('id')), str(target.get('id')), layer, _visual_shell_color(layer))
            if len(links) >= count:
                break
        if len(links) >= count:
            break

    index = 0
    while len(links) < count and particles:
        source = particles[index % len(particles)]
        target = particles[(index + 1) % len(particles)]
        add(str(source.get('id')), str(target.get('id')), str(source.get('layer') or 'macro'), _visual_shell_color(str(source.get('layer') or 'macro')))
        index += 1
    return links


def _find_stock_id(nodes: list[dict[str, Any]]) -> str:
    for node in nodes:
        if node.get('type') == 'stock':
            return str(node.get('id') or '')
    return str(nodes[0].get('id') or '') if nodes else ''


def _direct_neighbors(stock_id: str, links: list[dict[str, Any]]) -> set[str]:
    neighbors: set[str] = set()
    if not stock_id:
        return neighbors
    for link in links:
        source = _endpoint_id(link.get('source'))
        target = _endpoint_id(link.get('target'))
        if source == stock_id and target:
            neighbors.add(target)
        elif target == stock_id and source:
            neighbors.add(source)
    return neighbors


def _endpoint_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get('id') or '')
    return str(value or '')


def _canonical_layer(layer: str, node: dict[str, Any]) -> str:
    if node.get('forecast_role') == 'missing' or node.get('type') == 'missing_evidence':
        return 'missing'
    if node.get('forecast_role') == 'forecast':
        return 'forecast'
    return layer or 'news_event'


def _cluster_for_layer(layer: str) -> str:
    return _CLUSTER_BY_LAYER.get(layer, 'news')


def _node_color(layer: str, node: dict[str, Any]) -> str:
    node_type = str(node.get('type') or '')
    if node_type == 'stock':
        return '#f7fbff'
    if node_type == 'hot_money_seat':
        return '#27f5c5'
    if node_type == 'institutional_behavior':
        return '#f4d06f'
    if node.get('forecast_role') == 'missing' or node_type == 'missing_evidence':
        return '#6f7f99'
    if node.get('forecast_role') == 'forecast':
        return '#bffcff'
    return _LAYER_COLORS.get(layer, '#93a4c8')


def _link_color(layer: str, link: dict[str, Any]) -> str:
    if link.get('relation') == 'missing':
        return '#6f7f99'
    if layer == 'trading_behavior':
        return '#31f28a'
    if layer == 'risk':
        return '#ff4d5e'
    return _LAYER_COLORS.get(layer, '#2a9df4')


def _visual_shell_color(layer: str) -> str:
    cluster = _cluster_for_layer(layer)
    if cluster == 'trading':
        return '#1fcda5'
    if cluster in {'missing', 'risk'}:
        return '#355b84'
    if cluster == 'forecast':
        return '#7fefff'
    return '#2a9df4'


def _node_size(node: dict[str, Any]) -> float:
    node_type = str(node.get('type') or '')
    layer = str(node.get('layer') or '')
    weight = float(node.get('display_weight') or node.get('val') or 5)
    if node_type == 'stock':
        return 12.0
    if node.get('forecast_role') == 'forecast' or layer in {'forecast', 'expectation'}:
        return 5.2
    if node.get('forecast_role') == 'missing' or node_type == 'missing_evidence':
        return 5.4
    if layer == 'risk':
        return 6.0
    if node_type in {'fund_flow', 'hot_money_seat', 'institutional_behavior'}:
        return 5.8
    if layer in {'sector', 'financial', 'macro', 'policy', 'trading_behavior'}:
        return 4.4 + min(2.4, weight / 8.0)
    if layer == 'news_event':
        return 2.4 + min(1.4, weight / 12.0)
    return 3.2 + min(2.2, weight / 10.0)


def _node_role(node: dict[str, Any]) -> str:
    if node.get('type') == 'stock':
        return 'stock'
    if node.get('forecast_role') == 'forecast' or node.get('layer') in {'forecast', 'expectation'}:
        return 'forecast'
    if node.get('forecast_role') == 'missing' or node.get('type') == 'missing_evidence':
        return 'missing'
    if node.get('layer') == 'risk':
        return 'risk'
    if float(node.get('display_weight') or 0) >= 12 or float(node.get('analysis_weight') or 0) >= 0.65:
        return 'strong'
    return 'evidence'


def _anchor_for_layer(layer: str) -> tuple[float, float, float]:
    return _normalize(_LAYER_ANCHORS.get(layer, _LAYER_ANCHORS.get('news_event', (0.4, 0.4, 0.8))))


def _fibonacci_direction(index: int, total: int, seed: int) -> tuple[float, float, float]:
    total = max(1, total)
    offset = 2.0 / total
    increment = math.pi * (3.0 - math.sqrt(5.0))
    y = ((index * offset) - 1.0) + (offset / 2.0)
    radius = math.sqrt(max(0.0, 1.0 - y * y))
    phi = ((index + (seed % 37)) % total) * increment
    return _normalize((math.cos(phi) * radius, y, math.sin(phi) * radius))


def _jitter_vector(seed: int, index: int, layer_index: int, spread: float) -> tuple[float, float, float]:
    return (
        (_rand(seed, index, layer_index + 31) - 0.5) * spread,
        (_rand(seed, index, layer_index + 47) - 0.5) * spread,
        (_rand(seed, index, layer_index + 61) - 0.5) * spread,
    )


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2])
    if not length:
        return 0.0, 0.0, 1.0
    return vector[0] / length, vector[1] / length, vector[2] / length


def _distance_sq(left: dict[str, Any], right: dict[str, Any]) -> float:
    dx = float(left.get('x') or 0) - float(right.get('x') or 0)
    dy = float(left.get('y') or 0) - float(right.get('y') or 0)
    dz = float(left.get('z') or 0) - float(right.get('z') or 0)
    return dx * dx + dy * dy + dz * dz


def _stable_seed(*parts: str) -> int:
    digest = sha256('|'.join(parts).encode('utf-8', errors='ignore')).hexdigest()
    return int(digest[:12], 16)


def _rand(seed: int, index: int, salt: int) -> float:
    value = (seed + index * 1103515245 + salt * 12345) & 0x7fffffff
    value = (value ^ (value >> 13)) * 1274126177 & 0xffffffff
    return (value & 0xffff) / 0xffff
