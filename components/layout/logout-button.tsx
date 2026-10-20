'use client';

import { useRouter } from 'next/navigation';
import { LogOut } from 'lucide-react';
import { createClient } from '@/lib/supabase/client';

export function LogoutButton() {
  const router = useRouter();

  async function handleLogout() {
    const supabase = createClient();
    await supabase.auth.signOut();
    router.push('/login');
    router.refresh();
  }

  return (
    <button
      onClick={handleLogout}
      className="flex items-center gap-2 w-full px-3 py-2 rounded-md text-sm font-medium text-slate-500 hover:text-slate-300 hover:bg-slate-900 transition-colors"
    >
      <LogOut className="h-4 w-4" />
      Sign out
    </button>
  );
}
