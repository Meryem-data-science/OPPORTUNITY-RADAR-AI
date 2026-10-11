"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import Icon, { type IconName } from "@/components/icon";

const recommendationPaths = ["/recommendation", "/matching", "/priority", "/portfolio"];
const sourcePaths = ["/source-health", "/health"];
const navigation: { href: string; label: string; icon: IconName; paths?: string[] }[] = [
  { href: "/", label: "Accueil", icon: "home" },
  { href: "/explorer", label: "Offres", icon: "search" },
  { href: "/recommendation", label: "Recommandations", icon: "sparkles", paths: recommendationPaths },
  { href: "/applications", label: "Candidatures", icon: "folder" },
  { href: "/source-health", label: "Sources", icon: "sources", paths: sourcePaths },
];
const recommendationNavigation = [
  { href: "/recommendation", label: "Recommandations" },
  { href: "/matching", label: "Correspondances" },
  { href: "/priority", label: "Priorités" },
  { href: "/portfolio", label: "Portefeuille d’opportunités" },
];
const sourceNavigation = [
  { href: "/source-health", label: "État des sources" },
  { href: "/health", label: "État du système" },
];

export default function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const secondaryNavigation = recommendationPaths.includes(pathname)
    ? recommendationNavigation
    : sourcePaths.includes(pathname) ? sourceNavigation : null;

  return <div className="app-shell" onKeyDown={(event) => { if (event.key === "Escape") setMenuOpen(false); }}>
    <a className="skip-link" href="#app-content">Aller au contenu</a>
    <header className="app-mobile-header">
      <Link href="/" className="app-mobile-brand" onClick={() => setMenuOpen(false)}><Icon name="radar" />Opportunity Radar</Link>
      <button className="app-menu-toggle" type="button" aria-controls="app-sidebar" aria-expanded={menuOpen} aria-label={menuOpen ? "Fermer la navigation" : "Ouvrir la navigation"} onClick={() => setMenuOpen(!menuOpen)}>
        <Icon name={menuOpen ? "close" : "menu"} />
      </button>
    </header>
    {menuOpen && <button className="app-sidebar-backdrop" type="button" aria-label="Fermer la navigation" onClick={() => setMenuOpen(false)} />}
    <aside id="app-sidebar" className={`app-sidebar${menuOpen ? " is-open" : ""}`}>
      <Link className="app-brand" href="/" aria-label="Opportunity Radar, accueil" onClick={() => setMenuOpen(false)}>
        <span className="app-brand-mark"><Icon name="radar" size={28} /></span>
        <span><span className="app-brand-name">Opportunity<br />Radar<span className="app-brand-dot">.</span></span><span className="app-brand-caption">Data & IA · Maroc</span></span>
      </Link>
      <nav className="app-nav" aria-label="Navigation principale">
        {navigation.map((item) => {
          const active = item.paths ? item.paths.includes(pathname) : pathname === item.href || (item.href !== "/" && pathname.startsWith(`${item.href}/`)) || (item.href === "/explorer" && pathname.startsWith("/opportunities/"));
          return <Link key={item.href} href={item.href} className="app-nav-link" aria-current={active ? (pathname === item.href ? "page" : "location") : undefined} onClick={() => setMenuOpen(false)}>
            <Icon name={item.icon} className="app-nav-icon" /><span className="app-nav-label">{item.label}</span>
          </Link>;
        })}
      </nav>
      <div className="app-sidebar-foot"><span className="app-sidebar-foot-line" /><p>Une vue claire.<br />Des choix éclairés.</p><span>DATA · INTELLIGENCE ARTIFICIELLE</span></div>
    </aside>
    <div className="app-main" id="app-content" tabIndex={-1}>
      {secondaryNavigation && <nav className="app-subnav" aria-label={recommendationPaths.includes(pathname) ? "Analyses des opportunités" : "Supervision du radar"}>
        {secondaryNavigation.map((item) => <Link key={item.href} href={item.href} className="app-subnav-link" aria-current={pathname === item.href ? "page" : undefined}>{item.label}</Link>)}
      </nav>}
      {children}
    </div>
  </div>;
}
