import { 
  LayoutDashboard, 
  Bot, 
  BookOpen, 
  MessageSquare, 
  Users, 
  BarChart3, 
  Puzzle, 
  CreditCard, 
  Settings 
} from 'lucide-react';

export const navItems = [
  { name: 'Overview', href: '/dashboard', icon: LayoutDashboard },
  { name: 'Chatbots', href: '/chatbots', icon: Bot },
  { name: 'Knowledge', href: '/knowledge', icon: BookOpen },
  { name: 'Conversations', href: '/conversations', icon: MessageSquare },
  { name: 'Leads', href: '/leads', icon: Users },
  { name: 'Analytics', href: '/analytics', icon: BarChart3 },
  { name: 'Integrations', href: '/integrations', icon: Puzzle },
  { name: 'Billing', href: '/billing', icon: CreditCard },
  { name: 'Settings', href: '/settings', icon: Settings },
];
