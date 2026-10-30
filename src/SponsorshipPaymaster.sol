//SPDX-License-Identifier:MIT

pragma solidity ^0.8.0;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";



contract SponsorshipPaymaster is Ownable {


    error SponsorshipPaymaster_OnlyEntryPointAllowed();
    error SponsorshipPaymaster_RestrictedUser();
    error SponsorshipPaymaster_InsufficientBalance();
    error SponsorshipPaymaster_TransferFailed();

    mapping (address=> bool) private isWhitelisted;
    address immutable ENTRY_POINT;
    uint256 private balance;


    event WhitelistUpdated(address indexed user);
    event FundsDeposited(uint256 indexed amount);
    event TxSponsored(address indexed user);



    modifier onlyEntryPoint(){
        if (msg.sender!= ENTRY_POINT){
            revert SponsorshipPaymaster_OnlyEntryPointAllowed();
        }

        _;
    }


    constructor (address owner, address entryPoint ) Ownable(owner){
        ENTRY_POINT=entryPoint;
    } 


    function depositFunds() external payable  onlyOwner{

        
        emit FundsDeposited( msg.value);
       
    }


    function setWhitelistStatus(address user, bool status) external onlyOwner{
        
        isWhitelisted[user]= status;
        emit WhitelistUpdated(user);

    }


    function payForTx(address user, uint256 cost) external onlyEntryPoint{

        if(!isWhitelisted[user]) revert SponsorshipPaymaster_RestrictedUser();
        if(address(this).balance<cost) revert SponsorshipPaymaster_InsufficientBalance();
        (bool success,) = ENTRY_POINT.call{value:cost}("");

        if (!success) revert SponsorshipPaymaster_TransferFailed();

        emit TxSponsored(user);


    }


    function withdraw(uint256 amount) external onlyOwner {

         if(address(this).balance<amount) revert SponsorshipPaymaster_InsufficientBalance();
        (bool success,) = owner().call{value:amount}("");

        if (!success) revert SponsorshipPaymaster_TransferFailed();

        

    }


    receive() payable external {

      
        emit FundsDeposited( msg.value);

    }










}

