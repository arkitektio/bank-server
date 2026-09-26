"""The GraphQL operations this service sends to Scalable — and the only ones it may send.

Copied from the official CLI (``scalable-cli/src/broker_queries.rs``, ``overnight_queries.rs``,
``broker_shared.rs``, ``graphql.rs``). Everything is a read except the three 2FA-on-login
operations the login itself needs; :meth:`finance.scalable.client.ScalableClient.graphql`
refuses any operation not listed in :data:`OPERATIONS`, so no trade, savings-plan, watchlist or
portfolio-group mutation can ever leave this service.
"""

RESOLVE_BROKER_IDS = """
query ResolveBrokerIds($id: ID!) {
  account(id: $id) { id brokerPortfolios { id } }
}
"""

IS_2FA_ON_LOGIN_ENABLED = """
query Is2faOnLoginEnabled($input: Is2faOnLoginEnabledInput!) {
  is2faOnLoginEnabled(input: $input) { enabled hasApprovedSession }
}
"""

START_2FA_ON_LOGIN = """
mutation Start2faOnLogin($input: Start2faOnLoginInput!) {
  start2faOnLogin(input: $input) { mfaSessionId }
}
"""

VALIDATE_2FA_ON_LOGIN = """
mutation Validate2faOnLogin($input: Validate2faOnLoginInput!) {
  validate2faOnLogin(input: $input) { status }
}
"""

BROKER_OVERVIEW = """
query BrokerOverview($accountId: ID!, $portfolioId: ID!, $includeYearToDate: Boolean!) {
  account(id: $accountId) {
    brokerPortfolio(id: $portfolioId) {
      valuation(includeYearToDate: $includeYearToDate) {
        valuation
        securitiesValuation
        cryptoValuation
        timestampUtc { time }
      }
    }
  }
}
"""

BROKER_HOLDINGS = """
query BrokerHoldings($accountId: ID!, $portfolioId: ID!, $includeYearToDate: Boolean!, $quoteSource: MarketDataSource) {
  account(id: $accountId) {
    brokerPortfolio(id: $portfolioId) {
      inventory {
        items {
          isin
          name
          type
          inventory { position { filled pending blocked fifoPrice } }
          portfolioIsinPerformance { valuation currency }
          quoteTick(source: $quoteSource, includeYearToDate: $includeYearToDate) {
            midPrice
            currency
            timestampUtc { time }
            isOutdated
          }
        }
      }
    }
  }
}
"""

BROKER_LIMITS = """
query BrokerLimits($accountId: ID!, $portfolioId: ID!) {
  account(id: $accountId) {
    brokerPortfolio(id: $portfolioId) {
      payments {
        buyingPower { cashBalance cashAvailableToInvest }
        withdrawalPower { cashAvailableForWithdrawal }
      }
    }
  }
}
"""

BROKER_TRANSACTIONS = """
query BrokerTransactions($accountId: ID!, $portfolioId: ID!, $input: BrokerTransactionInput!) {
  account(id: $accountId) {
    brokerPortfolio(id: $portfolioId) {
      moreTransactions(input: $input) {
        cursor
        total
        transactions {
          __typename
          id
          currency
          type
          status
          isCancellation
          lastEventDateTime
          description
          ... on BrokerSecurityTransactionSummary { isin securityTransactionType quantity amount side }
          ... on BrokerCashTransactionSummary { relatedIsin cashTransactionType amount }
          ... on BrokerNonTradeSecurityTransactionSummary { isin nonTradeSecurityTransactionType quantity amount }
          ... on BrokerEltifTransactionSummary { isin securityTransactionType eltifQuantity amount side }
        }
      }
    }
  }
}
"""

DISCOVER_OVERNIGHT_ACCOUNTS = """
query DiscoverOvernightAccounts($accountId: ID!) {
  account(id: $accountId) {
    savingsAccounts { __typename id state personalizations { name } }
  }
}
"""

OVERNIGHT_SUMMARY = """
query OvernightSummary($accountId: ID!, $savingsAccountId: ID!) {
  account(id: $accountId) {
    savingsAccount(id: $savingsAccountId) {
      id
      ... on OvernightSavingsAccount { totalAmount }
    }
  }
}
"""

OVERNIGHT_TRANSACTIONS = """
query OvernightTransactions($accountId: ID!, $savingsAccountId: ID!, $input: SavingsAccountCashTransactionInput!) {
  account(id: $accountId) {
    savingsAccount(id: $savingsAccountId) {
      id
      moreTransactions(input: $input) {
        cursor
        total
        transactions { id currency type status isCancellation lastEventDateTime description cashTransactionType amount relatedIsin }
      }
    }
  }
}
"""

BROKER_CHART = """
query BrokerChart($isin: String!, $timeFrames: [TimeFrame!]!, $includeYearToDate: Boolean!) {
  timeSeriesBySecurity(isin: $isin, timeFrames: $timeFrames, includeYearToDate: $includeYearToDate) {
    isin
    timeFrame
    currency
    dataPoints { midPrice timestampUtc { time } }
  }
}
"""

BROKER_QUOTE = """
query BrokerQuote($accountId: ID!, $portfolioId: ID!, $isin: ID!, $includeYearToDate: Boolean!, $quoteSource: MarketDataSource) {
  account(id: $accountId) {
    brokerPortfolio(id: $portfolioId) {
      security(isin: $isin) {
        isin
        name
        quoteTick(source: $quoteSource, includeYearToDate: $includeYearToDate) {
          midPrice
          bidPrice
          askPrice
          currency
          isOutdated
          timestampUtc { time }
        }
      }
    }
  }
}
"""

OPERATIONS: dict[str, str] = {
    "ResolveBrokerIds": RESOLVE_BROKER_IDS,
    "Is2faOnLoginEnabled": IS_2FA_ON_LOGIN_ENABLED,
    "Start2faOnLogin": START_2FA_ON_LOGIN,
    "Validate2faOnLogin": VALIDATE_2FA_ON_LOGIN,
    "BrokerOverview": BROKER_OVERVIEW,
    "BrokerHoldings": BROKER_HOLDINGS,
    "BrokerLimits": BROKER_LIMITS,
    "BrokerTransactions": BROKER_TRANSACTIONS,
    "DiscoverOvernightAccounts": DISCOVER_OVERNIGHT_ACCOUNTS,
    "OvernightSummary": OVERNIGHT_SUMMARY,
    "OvernightTransactions": OVERNIGHT_TRANSACTIONS,
    "BrokerChart": BROKER_CHART,
    "BrokerQuote": BROKER_QUOTE,
}
