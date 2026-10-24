import type { CSSProperties } from "react";
import { theme } from "antd";

/** Follow the hosting domain's theme, including portalled detail drawers. */
export default function useInspectionPalette():CSSProperties {
  const {token}=theme.useToken();
  const color=token.colorBgContainer.replace("#","");
  const luminance=parseInt(color.slice(0,2),16)*.299+parseInt(color.slice(2,4),16)*.587+parseInt(color.slice(4,6),16)*.114;
  return {
    "--pi-line":token.colorBorderSecondary,"--pi-bg":token.colorBgContainer,"--pi-fg":token.colorText,
    "--pi-muted":token.colorTextSecondary,"--pi-accent":token.colorPrimary,
    "--pi-success":token.colorSuccess,"--pi-warning-color":token.colorWarning,"--pi-danger":token.colorError,
    "--text-primary":token.colorText,"--text-secondary":token.colorTextSecondary,"--border-color":token.colorBorderSecondary,
    "--bg-primary":token.colorBgContainer,"--bg-secondary":token.colorFillAlter,"--accent-color":token.colorPrimary,
    "--pi-color-scheme":luminance<128?"dark":"light",
  } as CSSProperties;
}
