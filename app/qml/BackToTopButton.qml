import QtQuick

// 「返回顶部」按钮。
//
// 实现已抽到 `FloatingActionButton`（右下角悬浮动作按钮的通用样式），
// 这里只保留"这是一个返回顶部按钮"的语义与默认值。
//
// **为什么不直接在页面里写 FloatingActionButton**：页面里出现
// `FloatingActionButton { icon: "upBold"; label: "返回顶部" }` 也能跑，
// 但 `objectName: "backToTopButton"` 这类诊断/探针依赖的标识、
// 以及"返回顶部"的文案就散在页面里了。保留这层薄封装后，
// 页面只表达意图、不重复视觉参数。
FloatingActionButton {
    id: root

    icon: "upBold"
    label: "返回顶部"
}
