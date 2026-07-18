import { createContext, useContext } from "react";

// Personal single-user build: account sharing was removed, so the viewing
// context is a static "always my own data" stub that keeps the original API
// shape for components written against it.
const ViewingContext = createContext({
  viewingEmail: null,
  viewingLabel: null,
  isViewingShared: false,
  sharedAccounts: [],
  myShares: [],
  defaultViewEmail: null,
  setViewingEmail: () => {},
  switchToOwn: () => {},
  setDefaultViewEmail: () => {},
  refreshShares: () => {},
});

export function ViewingProvider({ children }) {
  return (
    <ViewingContext.Provider
      value={{
        viewingEmail: null,
        viewingLabel: null,
        isViewingShared: false,
        sharedAccounts: [],
        myShares: [],
        defaultViewEmail: null,
        setViewingEmail: () => {},
        switchToOwn: () => {},
        setDefaultViewEmail: () => {},
        refreshShares: () => {},
      }}
    >
      {children}
    </ViewingContext.Provider>
  );
}

export function useViewing() {
  return useContext(ViewingContext);
}
