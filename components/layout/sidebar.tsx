import Link from 'next/link';
import { Bot } from 'lucide-react';
import { navItems } from '@/lib/navigation';
import { createClient } from '@/lib/supabase/server';
import { LogoutButton } from './logout-button';
import { OrgSwitcher } from '@/components/organization/org-switcher';

export async function Sidebar() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const displayName = user?.user_metadata?.full_name as string | undefined;
  const email = user?.email ?? '';
  const initials = displayName
    ? displayName.slice(0, 2).toUpperCase()
    : email.slice(0, 2).toUpperCase();

  return (
    <div className="flex flex-col w-64 border-r border-slate-800 bg-slate-950 h-screen sticky top-0 hidden md:flex shrink-0">
      <div className="p-6 pb-4">
        <div className="flex items-center gap-2">
          <div className="h-8 w-8 rounded-lg bg-blue-600 flex items-center justify-center">
            <Bot className="h-5 w-5 text-white" />
          </div>
          <span className="text-xl font-bold text-slate-100 tracking-tight">Aptlyst AI</span>
        </div>
      </div>
      <OrgSwitcher />
      <nav className="flex-1 px-4 py-3 space-y-1 overflow-y-auto">
        {navItems.map((item) => (
          <Link
            key={item.name}
            href={item.href}
            className="flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium text-slate-400 hover:text-slate-100 hover:bg-slate-900 transition-colors"
          >
            <item.icon className="h-4 w-4" />
            {item.name}
          </Link>
        ))}
      </nav>
      <div className="p-4 border-t border-slate-800 space-y-2">
        <div className="flex items-center gap-3 px-3 py-2">
          <div className="h-8 w-8 rounded-full bg-slate-800 flex items-center justify-center shrink-0">
            <span className="text-xs font-medium text-slate-300">{initials}</span>
          </div>
          <div className="flex flex-col min-w-0">
            <span className="text-sm font-medium text-slate-200 truncate">
              {displayName ?? email}
            </span>
            {displayName && (
              <span className="text-xs text-slate-500 truncate">{email}</span>
            )}
          </div>
        </div>
        <LogoutButton />
      </div>
    </div>
  );
}
