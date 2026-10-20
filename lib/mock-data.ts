import { Chatbot, Conversation, Lead } from './types';

export const mockChatbots: Chatbot[] = [
  {
    id: "bot_1",
    name: "Customer Support Bot",
    description: "Handles general customer inquiries.",
    status: "active",
    conversationsCount: 1245,
    leadsCount: 89,
    lastActive: new Date().toISOString(),
    websiteUrl: "https://example.com",
    welcomeMessage: "Hi there! How can I help you today?",
    primaryColor: "#0f172a"
  },
  {
    id: "bot_2",
    name: "Sales Assistant",
    description: "Qualifies leads and books meetings.",
    status: "active",
    conversationsCount: 832,
    leadsCount: 241,
    lastActive: new Date(Date.now() - 86400000).toISOString(),
    websiteUrl: "https://example.com/pricing",
    welcomeMessage: "Looking for pricing? I can help.",
    primaryColor: "#3b82f6"
  },
  {
    id: "bot_3",
    name: "Internal HR Bot",
    description: "Answers employee questions about policies.",
    status: "draft",
    conversationsCount: 0,
    leadsCount: 0,
    lastActive: new Date(Date.now() - 86400000 * 5).toISOString(),
  }
];

export const mockConversations: Conversation[] = [
  { id: "conv_1", chatbotId: "bot_1", userId: "usr_1", snippet: "How do I reset my password?", createdAt: new Date().toISOString() },
  { id: "conv_2", chatbotId: "bot_2", userId: "usr_2", snippet: "What are your enterprise features?", createdAt: new Date(Date.now() - 3600000).toISOString() },
  { id: "conv_3", chatbotId: "bot_1", userId: "usr_3", snippet: "My order hasn't arrived yet.", createdAt: new Date(Date.now() - 7200000).toISOString() },
];

export const mockLeads: Lead[] = [
  { id: "lead_1", chatbotId: "bot_2", email: "john@acme.corp", name: "John Doe", createdAt: new Date().toISOString() },
  { id: "lead_2", chatbotId: "bot_2", email: "jane@startup.io", name: "Jane Smith", createdAt: new Date(Date.now() - 86400000).toISOString() },
];

export const getDashboardStats = () => {
  return {
    totalChatbots: mockChatbots.length,
    totalConversations: mockChatbots.reduce((acc, bot) => acc + bot.conversationsCount, 0),
    totalLeads: mockChatbots.reduce((acc, bot) => acc + bot.leadsCount, 0),
  };
};
