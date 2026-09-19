import QtQuick

// 分段选择器：一排互斥选项，选中项用主题色淡底 + 描边标记。
//
// 用途：外观模式（白色简约 / 深色）、季数识别、多季展示、下载规则等
// 少量固定选项的场景。相比下拉框更直观，也更符合"简约线性"观感。
Item {
    id: root

    // [{ "label": "...", "value": "..." }]
    property var options: []
    property string currentValue: ""
    property int itemHeight: 32

    signal selected(string value)

    implicitWidth: row.width
    implicitHeight: itemHeight

    Row {
        id: row
        spacing: Theme.spacingSm

        Repeater {
            model: root.options

            delegate: Rectangle {
                id: seg
                required property var modelData

                readonly property bool active:
                    String(modelData.value) === String(root.currentValue)
                readonly property bool hovered: segMouse.containsMouse

                width: segLabel.implicitWidth + Theme.spacingLg * 2
                height: root.itemHeight
                radius: Theme.radiusSm

                color: active ? Theme.accentSoft
                     : hovered ? Theme.hoverFill
                     : Theme.fade(Theme.hoverFill)
                border.width: Theme.lineThin
                border.color: active ? Theme.accent : Theme.border

                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

                Text {
                    id: segLabel
                    anchors.centerIn: parent
                    text: seg.modelData.label
                    color: seg.active ? Theme.accent : Theme.textPrimary
                    font.pixelSize: Theme.fontMd

                    Behavior on color { ColorAnimation { duration: Theme.durFast } }
                }

                MouseArea {
                    id: segMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.selected(String(seg.modelData.value))
                }
            }
        }
    }
}
