import QtQuick
import QtQuick.Controls

// 主题色选择器：一排圆形色块，点击即切换整个界面的主题色。
//
// 关键点：切换只调用 Theme.applyAccent()，界面所有派生色
// （hover / pressed / 反色文字 / 淡底）都是 Theme 的绑定属性，会自动重算，
// 因此无需重启、无需重建页面。
Item {
    id: root

    property color current: Theme.accentSource
    property var presets: Theme.accentPresets

    signal picked(color value)

    implicitWidth: flow.width
    implicitHeight: flow.height

    Flow {
        id: flow
        spacing: Theme.spacingMd

        Repeater {
            model: root.presets

            delegate: Item {
                id: swatchHost
                required property var modelData

                readonly property color swatchColor: modelData.value
                readonly property bool isActive: {
                    // 颜色比较要用字符串化，避免浮点误差
                    return String(root.current).toLowerCase()
                        === String(swatchColor).toLowerCase()
                }

                width: 30
                height: 30

                // 色块主体
                Rectangle {
                    anchors.fill: parent
                    anchors.margins: swatchHost.isActive ? 3 : 0
                    radius: width / 2
                    color: swatchHost.swatchColor
                    border.width: swatchHost.isActive ? Theme.lineThick : Theme.lineThin
                    border.color: swatchHost.isActive ? Theme.textPrimary : Theme.border

                    Behavior on anchors.margins { NumberAnimation { duration: Theme.durFast } }
                    Behavior on border.width { NumberAnimation { duration: Theme.durFast } }
                }

                // 悬停放大
                scale: swatchMouse.containsMouse && !swatchHost.isActive ? 1.1 : 1.0
                Behavior on scale { NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic } }

                MouseArea {
                    id: swatchMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.picked(swatchHost.swatchColor)
                }

                ToolTip {
                    visible: swatchMouse.containsMouse
                    delay: 400
                    text: swatchHost.modelData.name
                }
            }
        }
    }
}
