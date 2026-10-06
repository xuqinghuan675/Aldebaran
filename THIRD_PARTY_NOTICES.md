# Third-Party Notices

Aldebaran is distributed under the MIT License except for third-party material that is explicitly identified below. Third-party components remain subject to their own license terms.

## Vendored browser assets

### Three.js

Vendored file:

- ui/assets/evidence_graph/vendor/three.min.js

Upstream project:

- three.js — https://github.com/mrdoob/three.js

The vendored file identifies itself as Three.js r160 and contains:

- Copyright 2010-2023 Three.js Authors
- SPDX-License-Identifier: MIT

MIT License text:

Copyright 2010-2023 Three.js Authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

### 3d-force-graph 1.73.4

Vendored file:

- ui/assets/evidence_graph/vendor/3d-force-graph.min.js

Upstream project:

- 3d-force-graph — https://github.com/vasturiano/3d-force-graph

Upstream license:

- MIT
- Copyright (c) 2017 Vasco Asturiano

MIT License text:

Copyright (c) 2017 Vasco Asturiano

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Architectural acknowledgment

Several files under core/agents/ acknowledge TradingAgents v0.2.4 as architectural inspiration.

- Upstream: https://github.com/TauricResearch/TradingAgents
- Upstream license: Apache License 2.0

Aldebaran does not include an upstream TradingAgents subtree or packaged copy. The acknowledgment is retained to make the architectural lineage visible.

## Technical-indicator compatibility module

core/utils/mytt.py is an original Aldebaran implementation of standard technical-analysis formulas and is covered by Aldebaran's MIT License. This release does not vendor source code from mpquant/MyTT.

## Python dependencies

Packages listed in requirements.txt are installed as dependencies and are not relicensed by Aldebaran. Each dependency remains governed by its upstream license and terms. When redistributing a binary build, review the licenses of all bundled dependencies, especially PySide6/Qt and any optional native/runtime packages.
