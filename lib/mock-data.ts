/**
 * @deprecated Legacy mock data — do not import in application code.
 * All application views use dynamic backend data or explicit empty states.
 */
import { MockChatbot, Conversation, Lead } from './types';

export const mockChatbots: MockChatbot[] = [];
export const mockConversations: Conversation[] = [];
export const mockLeads: Lead[] = [];

export const getDashboardStats = () => ({
  totalChatbots: 0,
  totalConversations: 0,
  totalLeads: 0,
});
