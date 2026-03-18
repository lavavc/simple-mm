'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

export interface DocEntry {
  slug: string;
  title: string;
}

export interface DocSection {
  title: string;
  entries: DocEntry[];
}

function SidebarLink({ slug, title }: DocEntry) {
  const pathname = usePathname();
  const href = slug === 'index' ? '/docs' : `/docs/${slug}`;
  const isActive = slug === 'index' ? pathname === '/docs' : pathname === `/docs/${slug}`;

  return (
    <Link
      href={href}
      className={`px-3 py-2 rounded-sm text-[11px] font-mono tracking-wide transition-colors ${
        isActive ? 'bg-white/[0.07] text-white' : 'text-white/40 hover:text-white/70 hover:bg-white/[0.04]'
      }`}
    >
      {title}
    </Link>
  );
}

export function DocsSidebar({ topLevel, sections }: { topLevel: DocEntry[]; sections: DocSection[] }) {
  return (
    <aside className="w-52 shrink-0">
      <p className="text-[9px] font-mono tracking-[0.2em] text-white/25 uppercase mb-4">Docs</p>
      <nav className="flex flex-col gap-0.5">
        {topLevel.map((entry) => (
          <SidebarLink key={entry.slug} {...entry} />
        ))}
        {sections.map((section) => (
          <div key={section.title} className="mt-4 flex flex-col gap-0.5">
            <p className="px-3 mb-1 text-[9px] font-mono tracking-[0.2em] text-white/25 uppercase">
              {section.title}
            </p>
            {section.entries.map((entry) => (
              <SidebarLink key={entry.slug} {...entry} />
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}
