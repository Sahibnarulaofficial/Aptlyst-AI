'use client';
import { useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Menu, X, Bot } from 'lucide-react';
import { cn } from '@/lib/utils';
import { navItems } from '@/lib/navigation';

import { LogoutButton } from './logout-button';
import { OrgSwitcher } from '@/components/organization/org-switcher';

export function MobileNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();

  return (
    <div className="md:hidden">
      <div className="flex items-center justify-between p-4 bg-slate-950 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <div className="h-8 w-8 rounded-lg bg-blue-600 flex items-center justify-center">
            <Bot className="h-5 w-5 text-white" />
          </div>
          <span className="text-xl font-bold text-slate-100 tracking-tight">Aptlyst AI</span>
        </div>
        <button 
          onClick={() => setOpen(!open)}
          className="p-2 text-slate-400 hover:text-slate-100"
        >
          {open ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
        </button>
      </div>

      {open && (
        <div className="fixed inset-0 top-[73px] z-50 bg-slate-950 border-t border-slate-800 flex flex-col justify-between">
          <div className="flex-1 overflow-y-auto">
            <OrgSwitcher />
            <nav className="p-4 space-y-1">
              {navItems.map((item) => (
                <Link
                  key={item.name}
                  href={item.href}
                  onClick={() => setOpen(false)}
                  className={cn(
                    "flex items-center gap-3 px-4 py-3 rounded-md text-base font-medium transition-colors",
                    pathname === item.href
                      ? "bg-slate-900 text-slate-100"
                      : "text-slate-400 hover:text-slate-100 hover:bg-slate-900/50"
                  )}
                >
                  <item.icon className="h-5 w-5" />
                  {item.name}
                </Link>
              ))}
            </nav>
          </div>
          <div className="p-4 border-t border-slate-800">
            <LogoutButton />
          </div>
        </div>
      )}
    </div>
  );
}
