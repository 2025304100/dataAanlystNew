import { forwardRef, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import "./inspectionSurfaces.css";

interface ConclusionProps {
  label: string;
  title: ReactNode;
  description?: ReactNode;
  icon: ReactNode;
  tone?: "neutral" | "warning" | "success" | "error";
  badge?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}

/** Presentation only: the caller retains all verdict and action eligibility rules. */
export function InspectionConclusion({ label, title, description, icon, tone = "neutral", badge, meta, actions, children }: ConclusionProps) {
  return <section className={`pi-surface-conclusion pi-surface-${tone}`} aria-label={label}>
    <div className="pi-surface-conclusion-heading"><span className="pi-surface-status-icon" aria-hidden="true">{icon}</span><div><span className="pi-surface-eyebrow">{label}</span><h3>{title}</h3></div>{badge && <span className="pi-surface-badge">{badge}</span>}</div>
    {description && <p className="pi-surface-description">{description}</p>}
    {children}
    {(meta || actions) && <div className="pi-surface-conclusion-bottom">{meta && <span className="pi-surface-meta">{meta}</span>}{actions && <div className="pi-surface-actions">{actions}</div>}</div>}
  </section>;
}

interface DisclosureProps {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  children: ReactNode;
  open?: boolean;
  className?: string;
}

export const InspectionDisclosure = forwardRef<HTMLDetailsElement, DisclosureProps>(function InspectionDisclosure({ title, description, icon, children, open, className = "" }, ref) {
  return <details ref={ref} open={open} className={`pi-surface-disclosure ${className}`}>
    <summary>{icon && <span className="pi-surface-disclosure-icon" aria-hidden="true">{icon}</span>}<span className="pi-surface-disclosure-label">{title}{description && <small>{description}</small>}</span><ChevronDown size={15} className="pi-surface-chevron" aria-hidden="true"/></summary>
    <div className="pi-surface-disclosure-content">{children}</div>
  </details>;
});
