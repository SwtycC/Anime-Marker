import QtQuick
import QtQuick.Shapes

// 线性矢量图标：描边 1.5px、无填充，与旧版 QPainter 自绘图标视觉一致。
//
// 实现说明：ShapePath 没有 visible 属性，因此不能用「一个 Shape 里放多个
// ShapePath + visible 切换」的写法。改为按 kind 分别声明独立的 Shape，
// 每个 Shape 整体控制 visible（Shape 继承自 Item，有 visible）。
// 坐标一律按 24×24 设计基准书写，再统一缩放。
Item {
    id: root

    property string kind: "grid"
    property color color: Theme.textSecondary
    property real strokeWidth: 1.5

    implicitWidth: 24
    implicitHeight: 24

    // 24×24 设计基准 → 实际尺寸的缩放比
    readonly property real _scale: Math.min(width, height) / 24

    // 统一缩放：所有子 Shape 铺满 root 后按比例缩放
    component IconShape: Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        transform: Scale {
            origin.x: 0
            origin.y: 0
            xScale: root._scale
            yScale: root._scale
        }
    }

    // ---- 海报墙：2×2 圆角小方格 ----
    IconShape {
        visible: root.kind === "grid"

        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            // 2×2 圆角方格（此处用直角即可，缩小后视觉一致）
            PathMove { x: 4; y: 4 }
            PathLine { x: 11;      y: 4 }
            PathLine { x: 11;      y: 11 }
            PathLine { x: 4;       y: 11 }
            PathLine { x: 4;       y: 4 }

            PathMove { x: 14; y: 4 }
            PathLine { x: 21;      y: 4 }
            PathLine { x: 21;      y: 11 }
            PathLine { x: 14;      y: 11 }
            PathLine { x: 14;      y: 4 }

            PathMove { x: 4; y: 14 }
            PathLine { x: 11;      y: 14 }
            PathLine { x: 11;      y: 21 }
            PathLine { x: 4;       y: 21 }
            PathLine { x: 4;       y: 14 }

            PathMove { x: 14; y: 14 }
            PathLine { x: 21;      y: 14 }
            PathLine { x: 21;      y: 21 }
            PathLine { x: 14;      y: 21 }
            PathLine { x: 14;      y: 14 }
        }
    }

    // ---- 在看：圆环 + 播放三角 ----
    IconShape {
        visible: root.kind === "play"
        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            startX: 20.5
            startY: 12
            PathAngleArc {
                centerX: 12; centerY: 12
                radiusX: 8.5; radiusY: 8.5
                startAngle: 0; sweepAngle: 360
            }
        }
        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            PathMove { x: 10; y: 8 }
            PathLine { x: 10;     y: 16 }
            PathLine { x: 16.5;   y: 12 }
            PathLine { x: 10;     y: 8 }
        }
    }

    // ---- 动态：时钟（圆 + 两根指针）----
    IconShape {
        visible: root.kind === "clock"
        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            startX: 20.5
            startY: 12
            PathAngleArc {
                centerX: 12; centerY: 12
                radiusX: 8.5; radiusY: 8.5
                startAngle: 0; sweepAngle: 360
            }
        }
        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            PathMove { x: 12; y: 12 }
            PathLine { x: 12;      y: 7 }
            PathMove { x: 12; y: 12 }
            PathLine { x: 16;      y: 13.5 }
        }
    }

    // ---- 设置：齿轮 ----
    // 结构：外圈 8 齿的齿圈（用 2 个同心圆 + 8 条短齿线表示）。
    // 关键比例：中心孔 3.5，齿圈外沿 8.5，齿线从 8.5 到 10.5（短），
    // 这样看起来是"齿轮"而不是"太阳/星号"。
    IconShape {
        visible: root.kind === "gear"

        // 中心孔
        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            startX: 15.5
            startY: 12
            PathAngleArc {
                centerX: 12; centerY: 12
                radiusX: 3.5; radiusY: 3.5
                startAngle: 0; sweepAngle: 360
            }
        }

        // 8 根短齿：从 r=8.5 向外到 r=10.5，每 45°
        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap

            PathMove { x: 12; y: 3.5 }
            PathLine { x: 12; y: 1.5 }

            PathMove { x: 18.01; y: 5.99 }
            PathLine { x: 19.42; y: 4.58 }

            PathMove { x: 20.5; y: 12 }
            PathLine { x: 22.5; y: 12 }

            PathMove { x: 18.01; y: 18.01 }
            PathLine { x: 19.42; y: 19.42 }

            PathMove { x: 12; y: 20.5 }
            PathLine { x: 12; y: 22.5 }

            PathMove { x: 5.99; y: 18.01 }
            PathLine { x: 4.58; y: 19.42 }

            PathMove { x: 3.5; y: 12 }
            PathLine { x: 1.5; y: 12 }

            PathMove { x: 5.99; y: 5.99 }
            PathLine { x: 4.58; y: 4.58 }
        }
    }

    // ---- 订阅：RSS 波纹（左下圆点 + 两道 1/4 圆弧）----
    // Qt 的 y 轴向下，1/4 圆弧从 0° 扫到 90° 即「右下 → 左下」方向，
    // 这里以左下角 (4,20) 为圆心，向右上画弧。
    IconShape {
        visible: root.kind === "rss"

        // 圆点
        ShapePath {
            strokeColor: "transparent"
            fillColor: root.color
            startX: 6
            startY: 19
            PathAngleArc {
                centerX: 5; centerY: 19
                radiusX: 1; radiusY: 1
                startAngle: 0; sweepAngle: 360
            }
        }

        // 内圈弧（半径 6）
        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            startX: 5
            startY: 19 - 6
            PathAngleArc {
                centerX: 5; centerY: 19
                radiusX: 6; radiusY: 6
                startAngle: 270; sweepAngle: 90
            }
        }

        // 外圈弧（半径 11.5）
        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            startX: 5
            startY: 19 - 11.5
            PathAngleArc {
                centerX: 5; centerY: 19
                radiusX: 11.5; radiusY: 11.5
                startAngle: 270; sweepAngle: 90
            }
        }
    }
}
