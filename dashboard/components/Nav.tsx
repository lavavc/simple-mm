'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  LayoutDashboard,
  TrendingUp,
  Building2,
  ArrowRightLeft,
  Wallet,
  Bell,
  BookOpen,
} from 'lucide-react';

const navItems = [
  { href: '/', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/prices', label: 'Prices', icon: TrendingUp },
  { href: '/venues', label: 'Venues', icon: Building2 },
  { href: '/arbitrage', label: 'Arbitrage', icon: ArrowRightLeft },
  { href: '/accounts', label: 'Accounts', icon: Wallet },
  { href: '/alerts', label: 'Alerts', icon: Bell },
  { href: '/docs', label: 'Docs', icon: BookOpen },
];

interface NavProps {
  unacknowledgedAlerts?: number;
}

export function Nav({ unacknowledgedAlerts = 0 }: NavProps) {
  const pathname = usePathname();

  return (
    <nav className="border-b border-white/[0.05] bg-[#0B0E14] text-slate-300">
      <div className="container mx-auto px-4">
        <div className="flex h-14 items-center justify-between">
          <div className="flex items-center gap-6">
            <Link href="/" className="font-bold text-lg text-white">
              CNGN Engine
            </Link>
            <div className="hidden md:flex items-center gap-1">
              {navItems.map((item) => {
                const Icon = item.icon;
                const isActive = item.href === '/' ? pathname === '/' : pathname.startsWith(item.href);
                return (
                  <Link key={item.href} href={item.href}>
                    <Button
                      variant="ghost"
                      size="sm"
                      className={cn(
                        'gap-2 hover:bg-white/5 hover:text-white',
                        isActive ? 'text-white bg-white/5 font-semibold' : 'text-slate-400'
                      )}
                    >
                      <Icon className="h-4 w-4" />
                      {item.label}
                      {item.href === '/alerts' && unacknowledgedAlerts > 0 && (
                        <Badge variant="destructive" className="ml-1 h-5 px-1.5">
                          {unacknowledgedAlerts}
                        </Badge>
                      )}
                    </Button>
                  </Link>
                );
              })}
            </div>
          </div>

          {/* Mobile nav */}
          <div className="md:hidden flex items-center gap-2">
            {navItems.slice(0, 4).map((item) => {
              const Icon = item.icon;
              const isActive = pathname === item.href;
              return (
                <Link key={item.href} href={item.href}>
                  <Button
                    variant={isActive ? 'secondary' : 'ghost'}
                    size="icon"
                    className="h-8 w-8"
                  >
                    <Icon className="h-4 w-4" />
                  </Button>
                </Link>
              );
            })}
          </div>
        </div>
      </div>
    </nav>
  );
}
